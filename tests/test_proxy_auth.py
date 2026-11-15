"""
The reverse proxy, against a real nginx.

This is the whole authentication story of the project: the application has no
users and no login, and what keeps a stranger out of the management interface
is deploy/nginx.conf. An API that hands out every device's SNMP community is
one `auth_basic off` away from being open, and a config file that is only ever
read by a human is exactly the kind of thing that drifts - so these tests run
the shipped config under a real nginx, with a fake application behind it, and
assert the three things that have to stay true:

  * nothing reaches the application without the password,
  * except the two health probes, which must stay open for the load balancer
    and for `topomon health`,
  * and a plain HTTP request is redirected before any password is sent, so the
    credentials never travel in clear.

The fake upstream records every request it is given, which is how "the request
never reached the application" is asserted rather than assumed: a 401 that
nginx produced after proxying would look identical from the client side.
"""
import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parent.parent / "deploy" / "nginx.conf"
USER = "admin"
PASSWORD = "not-the-real-one"


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


class _Upstream(BaseHTTPRequestHandler):
    """The application, reduced to "I was asked for this path"."""

    seen: list = []

    def do_GET(self):              # noqa: N802 - BaseHTTPRequestHandler's name
        type(self).seen.append(self.path)
        if self.path == "/health":
            body = {"status": "healthy"}
        elif self.path == "/health/ready":
            body = {"status": "ready", "database": "ok"}
        else:
            body = {"path": self.path, "reached_the_app": True}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest's output readable
        pass


# Paths inside the running proxy's directory that a test needs to reach
_PROXY_FILES = {}


@pytest.fixture(scope="session")
def proxy(tmp_path_factory):
    """
    (http_port, https_port) of a running nginx serving deploy/nginx.conf.

    Skips without an nginx to run, the same bargain tests/README.md describes
    for the SNMP simulator: a developer's machine stays usable, and CI sets
    REQUIRE_NGINX=1 so the same condition fails there instead of quietly
    testing nothing.
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

    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost",
         "-keyout", str(auth / "tls.key"), "-out", str(auth / "tls.crt")],
        check=True, capture_output=True,
    )
    hashed = subprocess.run(
        ["openssl", "passwd", "-apr1", PASSWORD],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (auth / "htpasswd").write_text(f"{USER}:{hashed}\n")

    # The application, on a port nginx will be pointed at
    upstream_port = _free_tcp_port()
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", upstream_port), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    http_port, https_port = _free_tcp_port(), _free_tcp_port()
    site = CONFIG.read_text()
    # Three substitutions, and nothing else: the directives under test are the
    # ones in the shipped file.
    site = site.replace("server app:8080;", f"server 127.0.0.1:{upstream_port};")
    site = site.replace("/etc/nginx/auth", str(auth))
    site = site.replace("listen 80 default_server;",
                        f"listen 127.0.0.1:{http_port} default_server;")
    site = site.replace("listen 443 ssl default_server;",
                        f"listen 127.0.0.1:{https_port} ssl default_server;")
    # The redirect names the port it is going to, which in production is the
    # default 443 and here is not.
    site = site.replace("return 301 https://$host$request_uri;",
                        f"return 301 https://$host:{https_port}$request_uri;")
    if _nginx_version(binary) < (1, 25, 1):
        # `http2 on;` replaced the listen flag in 1.25.1. The image this is
        # deployed with is pinned at 1.27 (docker-compose.yml); an older nginx
        # on a developer's machine should still be able to test the rest.
        site = site.replace("\n    http2 on;", "\n    # http2 on; (needs nginx 1.25.1)")
    (root / "site.conf").write_text(site)

    # nginx started as root runs its workers as "nobody", which then cannot
    # read a 0700 tmp directory. Same problem, same answer as the snmpsim
    # fixture in conftest.py.
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
        server.shutdown()
        pytest.fail(f"deploy/nginx.conf did not pass nginx -t:\n"
                    f"{check.stderr}{check.stdout}", pytrace=False)

    proc = subprocess.Popen([binary, "-c", str(root / "nginx.conf"),
                             "-g", "daemon off;"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_until_listening(https_port, proc, root / "error.log")
        # The password file, for the one test that makes it unreadable
        _PROXY_FILES["htpasswd"] = auth / "htpasswd"
        yield http_port, https_port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()


def _wait_until_listening(port, proc, log, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"nginx exited: {log.read_text() if log.exists() else ''}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"nginx never listened on {port}")


def get(url, user=None, password=None):
    """(status, body) - a redirect is a result here, not something to follow."""
    request = urllib.request.Request(url)
    if user is not None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# -- the password ----------------------------------------------------------


def test_the_interface_needs_a_password(proxy):
    """
    The one that matters. Without it, anybody who can reach port 443 can read
    every device's SNMP community and delete the device list.
    """
    _, https = proxy
    _Upstream.seen.clear()

    status, _ = get(f"https://127.0.0.1:{https}/api/v1/devices")

    assert status == 401
    # And it was refused *by the proxy* - the application was never asked
    assert _Upstream.seen == []


def test_the_right_password_gets_in(proxy):
    _, https = proxy
    _Upstream.seen.clear()

    status, body = get(f"https://127.0.0.1:{https}/api/v1/devices",
                       USER, PASSWORD)

    assert status == 200
    assert json.loads(body)["reached_the_app"] is True
    assert _Upstream.seen == ["/api/v1/devices"]


def test_a_wrong_password_is_no_better_than_none(proxy):
    _, https = proxy
    _Upstream.seen.clear()

    status, _ = get(f"https://127.0.0.1:{https}/api/v1/devices", USER, "guess")

    assert status == 401
    assert _Upstream.seen == []


def test_the_api_docs_need_it_too(proxy):
    """
    /docs is FastAPI's interactive documentation: it is not a document, it is
    a form that calls the API. Leaving it open would leave the API open.
    """
    _, https = proxy
    _Upstream.seen.clear()

    assert get(f"https://127.0.0.1:{https}/docs")[0] == 401
    assert _Upstream.seen == []


# -- the two exceptions ----------------------------------------------------


def test_the_health_probes_stay_open(proxy):
    """
    `topomon health` and any load balancer in front need these without a
    credential, which is also why app/main.py keeps the failure reason for the
    log instead of the response body.
    """
    http, https = proxy

    status, body = get(f"https://127.0.0.1:{https}/health/ready")
    assert status == 200
    assert json.loads(body) == {"status": "ready", "database": "ok"}

    # /health also over plain HTTP: not every probe will do TLS against a
    # self-signed certificate
    status, body = get(f"http://127.0.0.1:{http}/health")
    assert status == 200
    assert json.loads(body)["status"] == "healthy"


def test_readiness_is_not_a_way_in(proxy):
    """The exception is those two exact paths, not a prefix."""
    _, https = proxy
    _Upstream.seen.clear()

    assert get(f"https://127.0.0.1:{https}/health/../api/v1/devices")[0] in (401, 400)
    assert get(f"https://127.0.0.1:{https}/health/ready/../../api/v1/devices")[0] in (401, 400)
    assert _Upstream.seen == []


# -- the password must not travel in clear ---------------------------------


def test_plain_http_is_redirected_before_anything_is_sent(proxy):
    """
    Basic auth is base64, not encryption, so a credential sent over HTTP is a
    credential on the wire. The redirect happens before nginx asks for one.
    """
    http, https = proxy
    _Upstream.seen.clear()

    status, _ = get(f"http://127.0.0.1:{http}/devices")

    assert status == 301
    assert _Upstream.seen == []


def test_the_config_under_test_is_the_one_that_ships(proxy):
    """
    The fixture rewrites three things (the upstream address and the two
    ports). If it ever has to rewrite an auth_basic line to make the tests
    pass, the tests have stopped being about the shipped file.
    """
    text = CONFIG.read_text()
    assert "auth_basic_user_file /etc/nginx/auth/htpasswd;" in text
    assert "ssl_certificate     /etc/nginx/auth/tls.crt;" in text
    # Exactly one place asks for a password, and it is the catch-all location
    assert text.count("auth_basic ") == 1


if __name__ == "__main__":  # pragma: no cover - convenience
    sys.exit(pytest.main([__file__, "-v"]))


# -- a password file nginx cannot read ---------------------------------------


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root can read a file whatever its mode, so this proves nothing as root",
)
def test_an_unreadable_password_file_looks_healthy_until_someone_logs_in(proxy):
    """
    What shipped: auth/ was 0700 and htpasswd 0600, both owned by root, and
    the nginx worker that checks passwords is not root. So nobody could log
    in - and every probe said the deployment was fine, because nginx answers
    a request with *no* credentials with a 401 before it ever opens the file.

    This is that state, reproduced by taking the read bit away, and it is
    why `topomon health` and `topomon deploy` now probe with a wrong
    password (tools/topomon, auth_probe): 401 means the file was read and
    the password refused, 500 means nobody can get in.
    """
    _, https = proxy
    htpasswd = _PROXY_FILES["htpasswd"]
    mode = htpasswd.stat().st_mode
    htpasswd.chmod(0)
    try:
        without, _ = get(f"https://127.0.0.1:{https}/")
        wrong, _ = get(f"https://127.0.0.1:{https}/", "probe", "not-the-password")
        right, _ = get(f"https://127.0.0.1:{https}/", USER, PASSWORD)
    finally:
        htpasswd.chmod(mode)

    assert without == 401, "the unauthenticated probe cannot tell anything is wrong"
    assert wrong == 500, "a wrong password is what exposes it"
    assert right == 500, "and the right password does not get in either"

    # Readable again: the same probe now gets the answer that means "works"
    again, _ = get(f"https://127.0.0.1:{https}/", "probe", "not-the-password")
    assert again == 401
