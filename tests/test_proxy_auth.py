"""
The login, through the shipped reverse proxy, against the real application.

The login is the application's (app/core/security_middleware.py); the proxy
is what makes it the only way in (deploy/nginx.conf) and keeps passwords and
session cookies off the wire in clear. Either one can drift on its own - a
proxy line that stops forwarding X-Forwarded-Proto makes every cookie
non-Secure, a middleware change can open a path - so these tests run both
together: a real nginx with the shipped config, in front of the real
application started the way the systemd unit starts it, and assert what has
to stay true:

  * nothing but the health probes and the login page answers without a
    session: an API call is 401, a page is sent to the login page, /docs too
  * the session cookie is HttpOnly, SameSite and Secure - Secure because the
    application learns from the proxy that the request came over HTTPS
  * a changing request without X-Requested-With is refused (CSRF)
  * plain HTTP is redirected before anything is sent, the login form included
  * the two health probes stay open for load balancers and `mf-topomon doctor`
"""
import http.cookiejar
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "deploy" / "nginx.conf"
USER = "proxyadmin"
PASSWORD = "Not-the-real-one-42"


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _nginx_version(binary: str) -> tuple:
    """(major, minor, patch) of this nginx, or (0, 0, 0) if it cannot be read."""
    out = subprocess.run([binary, "-v"], capture_output=True, text=True)
    text = out.stderr + out.stdout
    digits = text.split("nginx/")[-1].split()[0] if "nginx/" in text else ""
    try:
        return tuple(int(part) for part in digits.split(".")[:3])
    except ValueError:
        return (0, 0, 0)


def _wait_until_listening(port, proc, log, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"exited: {log.read_text() if log.exists() else ''}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"never listened on {port}: {log.read_text() if log.exists() else ''}")


@pytest.fixture(scope="session")
def proxy(tmp_path_factory):
    """
    (http_port, https_port) of a running nginx serving deploy/nginx.conf, in
    front of the application on its own SQLite database.

    Skips without an nginx to run (tests/README.md); CI sets REQUIRE_NGINX=1
    so the same condition fails there instead of quietly testing nothing.
    """
    binary = os.environ.get("NGINX_BIN") or shutil.which("nginx")
    if not binary or not Path(binary).exists():
        message = f"no nginx to run: NGINX_BIN={binary!r}. See tests/README.md."
        if os.environ.get("REQUIRE_NGINX"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make the test certificate")

    root = tmp_path_factory.mktemp("proxy")
    auth = root / "auth"
    auth.mkdir()
    (root / "tmp").mkdir()
    (root / "data").mkdir()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost",
         "-keyout", str(auth / "tls.key"), "-out", str(auth / "tls.crt")],
        check=True, capture_output=True,
    )

    # The application, with an account, the way the unit starts it
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "DATABASE_URL": f"sqlite+aiosqlite:///{root}/app.db",
        "APP_DATA_DIR": str(root / "data"),
        "APP_SECRET_KEY": "0" * 64,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    created = subprocess.run(
        [sys.executable, "-m", "app.cli", "set-password", USER, "-"],
        input=PASSWORD + "\n", capture_output=True, text=True, cwd=ROOT, env=env,
    )
    assert created.returncode == 0, created.stderr
    app_port = _free_tcp_port()
    app_log = root / "app.log"
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(app_port), "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"],
        cwd=ROOT, env=env, stdout=open(app_log, "wb"), stderr=subprocess.STDOUT,
    )

    http_port, https_port = _free_tcp_port(), _free_tcp_port()
    site = CONFIG.read_text()
    # Four substitutions, and nothing else: the directives under test are the
    # ones in the shipped file.
    site = site.replace("server app:8080;", f"server 127.0.0.1:{app_port};")
    site = site.replace("/etc/nginx/auth", str(auth))
    site = site.replace("listen 80 default_server;",
                        f"listen 127.0.0.1:{http_port} default_server;")
    site = site.replace("listen 443 ssl default_server;",
                        f"listen 127.0.0.1:{https_port} ssl default_server;")
    site = site.replace("return 301 https://$host$request_uri;",
                        f"return 301 https://$host:{https_port}$request_uri;")
    if _nginx_version(binary) < (1, 25, 1):
        site = site.replace("\n    http2 on;", "\n    # http2 on; (needs nginx 1.25.1)")
    (root / "site.conf").write_text(site)

    as_root = "user root;\n" if getattr(os, "geteuid", lambda: 1)() == 0 else ""
    (root / "nginx.conf").write_text(f"""{as_root}worker_processes 1;
error_log {root}/error.log warn;
pid {root}/nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path {root}/tmp;
    proxy_temp_path {root}/tmp/proxy;
    fastcgi_temp_path {root}/tmp/fastcgi;
    uwsgi_temp_path {root}/tmp/uwsgi;
    scgi_temp_path {root}/tmp/scgi;
    include {root}/site.conf;
}}
""")
    check = subprocess.run([binary, "-t", "-c", str(root / "nginx.conf")],
                           capture_output=True, text=True)
    if check.returncode != 0:
        app.terminate()
        pytest.fail(f"deploy/nginx.conf did not pass nginx -t:\n{check.stderr}{check.stdout}",
                    pytrace=False)

    proc = subprocess.Popen([binary, "-c", str(root / "nginx.conf"), "-g", "daemon off;"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_until_listening(app_port, app, app_log)
        _wait_until_listening(https_port, proc, root / "error.log")
        yield http_port, https_port
    finally:
        for p in (proc, app):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener(jar=None):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    handlers = [_NoRedirect, urllib.request.HTTPSHandler(context=context)]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def call(url, method="GET", body=None, headers=None, jar=None):
    """(status, headers, body) - a redirect is a result here, not something to follow."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        request.add_header(k, v)
    try:
        with _opener(jar).open(request, timeout=15) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


CSRF = {"X-Requested-With": "tests"}


def login(https, jar):
    return call(f"https://127.0.0.1:{https}/api/v1/auth/login", "POST",
                {"username": USER, "password": PASSWORD}, CSRF, jar)


# -- nothing without a session -------------------------------------------------


def test_the_api_needs_a_login(proxy):
    """
    The one that matters. Without it, anybody who can reach port 443 can read
    every device's SNMP community and delete the device list.
    """
    _, https = proxy
    status, _, body = call(f"https://127.0.0.1:{https}/api/v1/devices")
    assert status == 401
    assert json.loads(body)["code"] == "not_authenticated"


def test_a_page_sends_the_browser_to_the_login_page(proxy):
    _, https = proxy
    status, headers, _ = call(f"https://127.0.0.1:{https}/devices", headers={"Accept": "text/html"})
    assert status == 303
    assert headers["Location"] == "/login?next=%2Fdevices"


def test_the_api_docs_need_it_too(proxy):
    """
    /docs is FastAPI's interactive documentation: it is not a document, it is
    a form that calls the API. Leaving it open would leave the API open.
    """
    _, https = proxy
    assert call(f"https://127.0.0.1:{https}/docs")[0] in (303, 401)
    assert call(f"https://127.0.0.1:{https}/openapi.json")[0] == 401


def test_the_static_files_behind_the_login_need_it(proxy):
    """Only what the login page itself needs is public."""
    _, https = proxy
    assert call(f"https://127.0.0.1:{https}/static/js/app.js")[0] == 401
    assert call(f"https://127.0.0.1:{https}/static/devices.html")[0] in (303, 401)
    assert call(f"https://127.0.0.1:{https}/static/js/login.js")[0] == 200
    assert call(f"https://127.0.0.1:{https}/login")[0] == 200


# -- logging in ----------------------------------------------------------------


def test_a_wrong_password_is_refused(proxy):
    _, https = proxy
    status, _, body = call(f"https://127.0.0.1:{https}/api/v1/auth/login", "POST",
                           {"username": USER, "password": "guess"}, CSRF)
    assert status == 401
    assert "detail" in json.loads(body)


def test_the_session_cookie_is_secure_httponly_and_samesite(proxy):
    """
    Secure only works if the application knows the request came over HTTPS:
    nginx says so in X-Forwarded-Proto and uvicorn trusts it from loopback
    (--proxy-headers, the systemd unit). Drop either and the cookie would be
    sent over plain HTTP too.
    """
    _, https = proxy
    status, headers, _ = login(https, http.cookiejar.CookieJar())
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("mft_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered


def test_a_session_gets_in_and_logging_out_ends_it(proxy):
    _, https = proxy
    jar = http.cookiejar.CookieJar()
    assert login(https, jar)[0] == 200
    status, _, body = call(f"https://127.0.0.1:{https}/api/v1/auth/me", jar=jar)
    assert status == 200
    assert json.loads(body)["username"] == USER
    assert call(f"https://127.0.0.1:{https}/api/v1/devices", jar=jar)[0] == 200
    assert call(f"https://127.0.0.1:{https}/api/v1/auth/logout", "POST", {}, CSRF, jar)[0] == 200
    assert call(f"https://127.0.0.1:{https}/api/v1/devices", jar=jar)[0] == 401


def test_a_change_without_the_csrf_header_is_refused(proxy):
    """
    A cross-site form can carry the cookie but cannot set a custom header -
    so a change without X-Requested-With is not from this UI.
    """
    _, https = proxy
    jar = http.cookiejar.CookieJar()
    assert login(https, jar)[0] == 200
    status, _, body = call(f"https://127.0.0.1:{https}/api/v1/groups", "POST", {"name": "x"}, jar=jar)
    assert status == 403
    assert json.loads(body)["code"] == "csrf"
    # Logging in is a change too (login CSRF)
    status, _, _ = call(f"https://127.0.0.1:{https}/api/v1/auth/login", "POST",
                        {"username": USER, "password": PASSWORD})
    assert status == 403


# -- the exceptions --------------------------------------------------------------


def test_the_health_probes_stay_open(proxy):
    http_port, https = proxy
    status, _, body = call(f"https://127.0.0.1:{https}/health/ready")
    assert status == 200
    assert json.loads(body) == {"status": "ready", "database": "ok"}
    # /health also over plain HTTP: not every probe will do TLS against a
    # self-signed certificate
    status, _, body = call(f"http://127.0.0.1:{http_port}/health")
    assert status == 200
    assert json.loads(body)["status"] == "healthy"


def test_readiness_is_not_a_way_in(proxy):
    """The exception is those two exact paths, not a prefix."""
    _, https = proxy
    assert call(f"https://127.0.0.1:{https}/health/../api/v1/devices")[0] in (401, 400)
    assert call(f"https://127.0.0.1:{https}/health/ready/../../api/v1/devices")[0] in (401, 400)


# -- nothing travels in clear ----------------------------------------------------


def test_plain_http_is_redirected_before_anything_is_sent(proxy):
    """A password or a cookie sent over HTTP is on the wire. The redirect comes first."""
    http_port, _ = proxy
    assert call(f"http://127.0.0.1:{http_port}/devices")[0] == 301
    assert call(f"http://127.0.0.1:{http_port}/login")[0] == 301
    status, headers, _ = call(f"http://127.0.0.1:{http_port}/api/v1/auth/login", "POST",
                              {"username": USER, "password": PASSWORD}, CSRF)
    assert status == 301
    assert "Set-Cookie" not in headers


def test_the_config_under_test_is_the_one_that_ships(proxy):
    """
    The fixture rewrites the upstream, the certificate directory and the
    ports. The proxy no longer authenticates - that is the application's -
    but it must keep telling the application the request came over HTTPS.
    """
    text = CONFIG.read_text()
    assert "auth_basic" not in text
    assert "ssl_certificate     /etc/nginx/auth/tls.crt;" in text
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in text


if __name__ == "__main__":  # pragma: no cover - convenience
    sys.exit(pytest.main([__file__, "-v"]))
