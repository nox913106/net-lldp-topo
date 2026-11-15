"""
The deployment surface: install.sh, tools/topomon, docker-compose.yml.

A deployment script is the one piece of a project that runs exactly once per
machine, usually at the worst moment, usually as root, usually with nobody
watching the output. There is no way to unit test "it deployed", but three
classes of failure in it are cheap to catch here and expensive to catch on a
customer's server:

  * a shell syntax error, which fails *after* half the work is done,
  * a command that exists but was never wired into the dispatcher (or the
    other way round: documented in the usage text but not implemented),
  * docker-compose.yml publishing a port to the world again. The application
    has no authentication of its own, so "127.0.0.1" in front of 8080 and
    5432 is load-bearing, and it is exactly the kind of line that gets
    "temporarily" removed while debugging.

What is *not* here: a real deployment. That needs a docker daemon and several
minutes, and it is the one thing CI cannot do for free. docs/DEPLOYMENT.md
says what was and was not exercised.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# Three scripts and a shim. The native tool is the primary way to run this
# (apt + systemd, like the project it is modelled on); the Docker tool is
# the secondary one; bootstrap is what the one-line install fetches and it
# hands over to one of the two; the shim keeps /usr/local/bin/topomon on
# machines deployed under the old name pointing at something.
NATIVE = ROOT / "scripts" / "mf-topomon.sh"
DOCKER = ROOT / "scripts" / "mf-topomon-docker.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"
SHIM = ROOT / "tools" / "topomon"
TOOLS = [NATIVE, DOCKER]
# The Docker-specific tests further down were written against this name
TOPOMON = DOCKER
INSTALL = BOOTSTRAP
COMPOSE = ROOT / "docker-compose.yml"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")

# The scripts run with `set -u`, so a variable they forgot to guard is a hard
# error - but only on a machine where it is unset. This session's shell had
# GITHUB_TOKEN exported, which made `topomon --help` work here and fail in CI
# with "GITHUB_TOKEN: unbound variable". So the CLI is always run with a
# deliberately bare environment: PATH and HOME, nothing else.
CLEAN_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/root"),
}


def run_cli(*args, env=None, tool=None):
    return subprocess.run([str(tool or TOPOMON), *args], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=60, env=env or CLEAN_ENV)


def command_name(tool):
    """What an operator types: mf-topomon, mf-topomon-docker"""
    return tool.name[:-len(".sh")]


@pytest.fixture(scope="module")
def compose_file():
    return yaml.safe_load(COMPOSE.read_text())


# -- the scripts parse and dispatch ---------------------------------------


@needs_bash
@pytest.mark.parametrize("script", [NATIVE, DOCKER, BOOTSTRAP, SHIM], ids=lambda p: p.name)
def test_the_script_parses(script):
    """`bash -n` is not a lint, it is the difference between failing at line 1
    and failing at line 200 with the containers half started."""
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@needs_bash
def test_the_scripts_are_executable():
    # curl | sudo bash does not care, but `./install.sh` and the symlink in
    # /usr/local/bin do
    for script in (NATIVE, DOCKER, BOOTSTRAP, SHIM):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda p: p.name)
def test_every_command_is_reachable_and_documented(tool):
    """
    Three lists that drift apart on their own: the cmd_* functions, the case
    that dispatches them, and the usage text an operator reads.
    """
    text = tool.read_text()
    implemented = set(re.findall(r"^cmd_([a-z]+)\(\)", text, re.MULTILINE))
    dispatched = set(re.findall(r"^\s+([a-z|]+)\)\s+cmd_[a-z]+", text, re.MULTILINE))
    dispatched = {name for group in dispatched for name in group.split("|")}

    assert implemented, "no cmd_* functions found - did the script move?"
    assert implemented == dispatched, (
        f"implemented but not dispatched: {sorted(implemented - dispatched)}; "
        f"dispatched but not implemented: {sorted(dispatched - implemented)}"
    )

    usage = text.split("usage() {", 1)[1].split("\nEOF\n", 1)[0]
    for name in implemented:
        assert f"{command_name(tool)} {name}" in usage, (
            f"{name} is missing from the usage text"
        )


def test_the_two_tools_answer_to_the_same_questions():
    """
    Whichever way it was installed, the same handful of words get an
    operator through the day - only the three that name the mechanism
    differ (install/deploy, upgrade/update, doctor/health).
    """
    def commands(tool):
        return set(re.findall(r"^cmd_([a-z]+)\(\)", tool.read_text(), re.MULTILINE))

    native, docker = commands(NATIVE), commands(DOCKER)
    assert native - docker == {"install", "upgrade", "doctor"}
    assert docker - native == {"deploy", "update", "health"}


@needs_bash
@pytest.mark.parametrize("tool", TOOLS, ids=lambda p: p.name)
@pytest.mark.parametrize("args,expected", [
    ([], 2),                    # no command: print usage, and say so in $?
    (["frobnicate"], 2),        # a typo must not be mistaken for a default
    (["--help"], 0),
    (["version"], 0),           # the one informational command that needs nothing
])
def test_the_dispatcher_exits_the_way_a_script_would_expect(tool, args, expected):
    result = run_cli(*args, tool=tool)
    assert result.returncode == expected, result.stdout + result.stderr


@needs_bash
@pytest.mark.parametrize("tool", TOOLS + [SHIM], ids=lambda p: p.name)
def test_version_names_the_checkout(tool):
    result = run_cli("version", tool=tool)
    assert "commit" in result.stdout
    assert str(ROOT) in result.stdout


@needs_bash
def test_nothing_depends_on_a_variable_the_developer_happens_to_have():
    """
    The bug this exists for, in full: the usage text is a heredoc with an
    unquoted delimiter, so `$GITHUB_TOKEN` inside the documented install
    command was *expanded* rather than printed - and under `set -u` an unset
    variable there aborts the script. `topomon --help` therefore exited 1 on
    every machine that did not happen to have GITHUB_TOKEN exported. It
    passed locally and failed in CI.

    So: the two commands that need no docker must work with nothing in the
    environment but PATH and HOME.
    """
    for tool in TOOLS:
        for args in (["--help"], ["version"]):
            result = run_cli(*args, tool=tool)
            assert result.returncode == 0, (
                f"{tool.name} {' '.join(args)} needs something from the "
                f"environment: {result.stderr.strip()}"
            )
            assert "unbound variable" not in result.stderr


def test_bootstrap_hands_over_to_the_tool_in_the_repo():
    """
    bootstrap.sh puts the code on the machine and then execs the tool that
    is in it. If it grew its own copy of the install steps, the two would
    drift and only one of them would be the tested one.
    """
    text = BOOTSTRAP.read_text()
    assert 'exec "$MF_TOPOMON_DIR/scripts/mf-topomon.sh" install' in text
    assert 'exec "$MF_TOPOMON_DIR/scripts/mf-topomon-docker.sh" deploy' in text
    for step in ("docker compose up", "systemctl", "pip install", "createdb"):
        assert step not in text, f"bootstrap.sh does {step} itself"
    # It is piped into bash, so it must not read from stdin, and it must only
    # act once the whole file has arrived (hence a single call at the end).
    assert text.rstrip().endswith('main "$@"')
    assert "read -r" not in text


def test_the_native_install_is_the_default():
    text = BOOTSTRAP.read_text()
    assert 'MF_TOPOMON_MODE="${MF_TOPOMON_MODE:-native}"' in text


def test_the_documented_one_liner_fetches_a_file_that_exists():
    """
    The repository is public, so the install command is one line with no
    token in it - and the path in that line has to be a file in the tree.
    """
    url = "https://raw.githubusercontent.com/nox913106/net-lldp-topo/main/scripts/bootstrap.sh"
    assert (ROOT / "scripts" / "bootstrap.sh").is_file()
    for doc in (ROOT / "README.md", ROOT / "docs" / "DEPLOYMENT.md",
                ROOT / "docs" / "index.html", NATIVE, BOOTSTRAP):
        assert url in doc.read_text(), f"{doc.name} does not show the install command"


def test_nothing_still_asks_for_a_token():
    """
    While the repository was private the installer carried a GitHub token
    through to .git/config. None of that is needed now, and an install
    command that still mentions a token reads as "you need one".
    """
    for path in (NATIVE, DOCKER, BOOTSTRAP, ROOT / "README.md",
                 ROOT / "docs" / "DEPLOYMENT.md"):
        text = path.read_text()
        for word in ("GITHUB_TOKEN", "x-access-token", "Authorization: Bearer"):
            assert word not in text, f"{path.name} still mentions {word}"


def test_the_old_command_still_reaches_the_tool():
    """
    Machines deployed under the old name have /usr/local/bin/topomon linked
    to tools/topomon. After their next update that path must still be a
    working command, not a link to a file that has gone.
    """
    assert "scripts/mf-topomon-docker.sh" in SHIM.read_text()
    result = run_cli("--help", tool=SHIM)
    assert result.returncode == 0
    assert "mf-topomon-docker deploy" in result.stdout


# -- the native install ------------------------------------------------------
#
# Everything below was first established by running it: a one-line install
# on a clean machine, then login, doctor, backup, restore, upgrade, a
# deliberately broken upgrade that has to roll back, uninstall, reinstall
# over the surviving data, and purge (docs/PERFORMANCE.md section 44). What
# is pinned here is the part of that which can be checked without a machine
# to install on.

UNITS = sorted((ROOT / "deploy" / "systemd").glob("*.service"))


def test_there_are_two_services():
    assert [unit.name for unit in UNITS] == [
        "mf-topomon-app.service", "mf-topomon-collector.service"
    ]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_placeholder_in_a_unit_is_filled_in(unit):
    """A placeholder the installer does not know would be written out as-is"""
    wanted = set(re.findall(r"@[A-Z_]+@", unit.read_text()))
    filled = set(re.findall(r's\|(@[A-Z_]+@)\|', NATIVE.read_text()))
    assert wanted, "the unit has no placeholders - is it still a template?"
    assert wanted <= filled, f"not substituted: {sorted(wanted - filled)}"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_the_services_do_not_run_as_root(unit):
    text = unit.read_text()
    assert "User=@RUN_USER@" in text
    assert "NoNewPrivileges=true" in text
    # The account can read its code and write its data directory, no more
    assert "ProtectSystem=full" in text
    assert "ReadWritePaths=@DATA_DIR@" in text
    assert "Restart=always" in text


def test_the_application_listens_on_loopback_only():
    """
    Authentication is nginx's. An application reachable on the network
    without going through it has no authentication at all.
    """
    text = (ROOT / "deploy" / "systemd" / "mf-topomon-app.service").read_text()
    assert "--host 127.0.0.1" in text
    assert "0.0.0.0" not in text
    assert "--reload" not in text


def test_the_collector_can_bind_the_trap_port_without_being_root():
    text = (ROOT / "deploy" / "systemd" / "mf-topomon-collector.service").read_text()
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in text
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in text


def test_the_native_password_file_is_readable_by_the_proxy_worker():
    """The same mistake the Docker tool made, not made again here"""
    text = NATIVE.read_text()
    body = text[text.index("secure_auth_dir() {"):text.index("print_new_credentials() {")]
    assert 'chown "root:$group" "$AUTH_DIR/htpasswd"' in body
    assert 'chmod 640 "$AUTH_DIR/htpasswd"' in body
    assert 'chmod 750 "$AUTH_DIR"' in body
    assert 'chmod 600 "$AUTH_DIR/tls.key"' in body
    for function in ("ensure_auth() {", "cmd_password() {"):
        start = text.index(function)
        assert "secure_auth_dir" in text[start:text.index("\n}\n", start)], function
    for function in ("cmd_install() {", "cmd_doctor() {"):
        start = text.index(function)
        assert ("require_login_works" in text[start:text.index("\n}\n", start)]
                or "auth_probe" in text[start:text.index("\n}\n", start)]), function


def test_the_first_password_is_kept_where_only_root_can_read_it():
    """
    "Shown once" is not enough when the install ran with its output piped
    away. So it is also written down - with a umask that keeps it to root -
    and it stops being true the moment the password is changed, so that is
    when the file goes.
    """
    text = NATIVE.read_text()
    assert '(umask 077 && printf \'%s / %s\\n\' "$user" "$password" > "$INITIAL_PASSWORD_FILE")' in text
    password = text[text.index("cmd_password() {"):text.index("\n}\n", text.index("cmd_password() {"))]
    assert 'rm -f "$INITIAL_PASSWORD_FILE"' in password
    doctor = text[text.index("cmd_doctor() {"):text.index("\n}\n", text.index("cmd_doctor() {"))]
    assert "INITIAL_PASSWORD_FILE" in doctor, "doctor should nag while it is still there"


def test_every_failed_check_comes_with_a_command_to_run():
    """
    The reason `doctor` is called that: "what is wrong" without "so type
    this" is half an answer. Every bad() in it takes the fix as its second
    argument.
    """
    text = NATIVE.read_text()
    doctor = text[text.index("cmd_doctor() {"):text.index("\n}\n", text.index("cmd_doctor() {"))]
    calls = re.findall(r'\n\s+bad "((?:[^"\\]|\\.)*)"((?:\s*\\\n)?\s*"(?:[^"\\]|\\.)*")?', doctor)
    assert len(calls) >= 8, "found only %d checks that can fail" % len(calls)
    without = [message for message, fix in calls if not fix.strip()]
    assert without == [], f"no fix command for: {without}"


def test_an_upgrade_backs_up_first_and_rolls_back_on_failure():
    text = NATIVE.read_text()
    upgrade = text[text.index("cmd_upgrade() {"):text.index("\n}\n", text.index("cmd_upgrade() {"))]
    assert upgrade.index("cmd_backup") < upgrade.index("converge"), (
        "the backup has to be taken before anything is changed"
    )
    assert 'git reset --hard --quiet "$before"' in upgrade
    assert "auth_probe" in upgrade, "an upgrade that breaks login must roll back"
    assert "trap print_new_credentials EXIT" in upgrade


def test_install_and_upgrade_run_the_same_steps():
    """One list of steps, so a repair and an upgrade cannot diverge"""
    text = NATIVE.read_text()
    for function in ("cmd_install() {", "cmd_upgrade() {"):
        start = text.index(function)
        assert "\n    converge\n" in text[start:text.index("\n}\n", start)], function


@needs_bash
def test_the_installed_proxy_config_is_the_tested_one_with_three_things_changed(tmp_path):
    """
    tests/test_proxy_auth.py runs deploy/nginx.conf. What the native install
    writes into nginx is that file with the upstream, the certificate
    directory and the ports changed - rendered here by the tool itself.
    """
    if shutil.which("nginx") is None:
        pytest.skip("the renderer asks nginx for its version")
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "mf-topomon.env").write_text("TOPOMON_HTTP_PORT=8088\nTOPOMON_HTTPS_PORT=8443\n")
    env = dict(CLEAN_ENV, MF_TOPOMON_ETC=str(etc), MF_TOPOMON_APP_PORT="9090")
    env["PATH"] = env["PATH"] + ":/usr/sbin:/sbin"
    result = run_cli("render-nginx", tool=NATIVE, env=env)
    assert result.returncode == 0, result.stderr
    site = result.stdout

    assert "server 127.0.0.1:9090;" in site
    assert "server app:8080;" not in site
    assert f"auth_basic_user_file {etc}/auth/htpasswd;" in site
    assert f"ssl_certificate_key {etc}/auth/tls.key;" in site
    assert "/etc/nginx/auth" not in site
    assert "listen 8088 default_server;" in site
    assert re.search(r"listen 8443 ssl( http2)? default_server;", site)
    # Either spelling of HTTP/2, never both and never neither
    assert ("http2 on;" in site) != ("ssl http2 default_server;" in site)

    # Everything else is the shipped file, line for line
    shipped = (ROOT / "deploy" / "nginx.conf").read_text().splitlines()
    rendered = site.splitlines()
    changed = [line for line in shipped if line not in rendered]
    assert all(
        any(word in line for word in ("app:8080", "/etc/nginx/auth", "listen ", "http2 on;"))
        for line in changed
    ), [line for line in changed
        if not any(w in line for w in ("app:8080", "/etc/nginx/auth", "listen ", "http2 on;"))]


# -- what docker-compose.yml must keep being ------------------------------


def test_the_application_is_not_published_to_the_network(compose_file):
    """
    The application has no login. Every published port that reaches it has to
    be either the proxy (which asks for a password) or localhost only.
    """
    open_to_the_world = []
    for name, service in compose_file["services"].items():
        for mapping in service.get("ports", []):
            published = str(mapping).split(":")
            # "127.0.0.1:5432:5432" is bound; "5432:5432" is not
            if len(published) < 3 or not published[0].startswith("127.0.0.1"):
                open_to_the_world.append(f"{name}: {mapping}")

    assert open_to_the_world == [
        "proxy: ${TOPOMON_HTTP_PORT:-80}:80",
        "proxy: ${TOPOMON_HTTPS_PORT:-443}:443",
    ], f"published without authentication in front: {open_to_the_world}"


def test_the_proxy_serves_the_config_that_is_tested(compose_file):
    """tests/test_proxy_auth.py tests deploy/nginx.conf; this is what makes
    that the file the proxy actually runs."""
    mounts = compose_file["services"]["proxy"]["volumes"]
    assert "./deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" in mounts
    # Read-only: a compromised proxy container must not be able to rewrite its
    # own credentials file or its key
    assert "./auth:/etc/nginx/auth:ro" in mounts


def test_the_application_runs_without_the_reload_watcher(compose_file):
    command = compose_file["services"]["app"]["command"]
    assert "uvicorn" in command
    assert "--reload" not in command, "that is the development server"
    # Behind a proxy, without this every log line and every rate limit sees
    # the proxy's address instead of the client's
    assert "--proxy-headers" in command


def test_every_service_that_can_fail_quietly_restarts(compose_file):
    for name, service in compose_file["services"].items():
        assert service.get("restart") == "unless-stopped", name


def test_the_proxy_waits_for_the_application(compose_file):
    """nginx resolves its upstream at startup: started before app, it exits
    with "host not found in upstream" and never comes back on its own."""
    assert compose_file["services"]["proxy"]["depends_on"]["app"] == {
        "condition": "service_healthy"
    }
    assert "healthcheck" in compose_file["services"]["app"]


# -- the password file ----------------------------------------------------
#
# Found by deploying for real and trying to log in: `topomon deploy` reported
# success, `topomon health` said everything was fine, and the right password
# got a 500. tests/test_proxy_auth.py reproduces the mechanism under a real
# nginx; these pin the three things the tool does about it.


def test_the_proxy_worker_is_given_the_password_file():
    """
    htpasswd is read by the nginx worker on every request, and the worker is
    not root. 0600 root in a 0700 directory - what this used to create - is
    a file it cannot open.
    """
    text = TOPOMON.read_text()
    body = text[text.index("secure_auth_dir() {"):text.index("print_new_credentials() {")]
    assert 'chown "root:$NGINX_GID" "$AUTH_DIR"' in body
    assert 'chmod 750 "$AUTH_DIR"' in body
    assert 'chown "root:$NGINX_GID" "$AUTH_DIR/htpasswd"' in body
    assert 'chmod 640 "$AUTH_DIR/htpasswd"' in body
    # Group-readable, not world-readable: it holds password hashes
    assert not re.search(r"chmod (644|755|777) \"\$AUTH_DIR(/htpasswd)?\"", text)
    # ...and nothing puts it back the way it was
    assert 'chmod 700 "$AUTH_DIR"' not in text
    assert 'chmod 600 "$AUTH_DIR/htpasswd"' not in text
    # Both writers of the file fix its ownership afterwards
    for function in ("ensure_auth() {", "cmd_password() {"):
        start = text.index(function)
        assert "secure_auth_dir" in text[start:text.index("\n}\n", start)], function


def test_the_worker_group_matches_the_image_the_compose_file_pins(compose_file):
    """101 is nginx's group in the nginx:1.27 images; another image, another id"""
    assert compose_file["services"]["proxy"]["image"].startswith("nginx:1.27")
    assert 'NGINX_GID="${TOPOMON_NGINX_GID:-101}"' in TOPOMON.read_text()


def test_health_and_deploy_try_a_password_not_just_the_absence_of_one():
    text = TOPOMON.read_text()
    probe = text[text.index("auth_probe() {"):text.index("# 指令", text.index("auth_probe() {"))]
    assert "-u " in probe, "the probe has to send credentials"
    for function in ("cmd_health() {", "require_login_works() {"):
        start = text.index(function)
        assert "auth_probe" in text[start:text.index("\n}\n", start)], function
    deploy = text[text.index("cmd_deploy() {"):text.index("\n}\n", text.index("cmd_deploy() {"))]
    assert "require_login_works" in deploy
    update = text[text.index("cmd_update() {"):text.index("\n}\n", text.index("cmd_update() {"))]
    assert "auth_probe" in update, "an update that breaks login must roll back"


def test_a_generated_password_is_shown_even_when_the_deploy_fails():
    """
    The password is written to htpasswd before the containers are built. A
    first deploy that failed after that point exited without printing it,
    and the next run found htpasswd already there and generated nothing -
    so the password "shown only once" was shown zero times.
    """
    text = TOPOMON.read_text()
    for function in ("cmd_deploy() {", "cmd_update() {"):
        start = text.index(function)
        assert "trap print_new_credentials EXIT" in text[start:text.index("\n}\n", start)], function
