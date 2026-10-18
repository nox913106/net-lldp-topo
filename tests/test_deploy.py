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
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TOPOMON = ROOT / "tools" / "topomon"
INSTALL = ROOT / "install.sh"
COMPOSE = ROOT / "docker-compose.yml"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")


@pytest.fixture(scope="module")
def compose_file():
    return yaml.safe_load(COMPOSE.read_text())


# -- the scripts parse and dispatch ---------------------------------------


@needs_bash
@pytest.mark.parametrize("script", [TOPOMON, INSTALL], ids=lambda p: p.name)
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
    for script in (TOPOMON, INSTALL):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_every_command_is_reachable_and_documented():
    """
    Three lists that drift apart on their own: the cmd_* functions, the case
    that dispatches them, and the usage text an operator reads.
    """
    text = TOPOMON.read_text()
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
        assert f"topomon {name}" in usage, f"{name} is missing from the usage text"


@needs_bash
@pytest.mark.parametrize("args,expected", [
    ([], 2),                    # no command: print usage, and say so in $?
    (["frobnicate"], 2),        # a typo must not be mistaken for a default
    (["--help"], 0),
    (["version"], 0),           # the one informational command that needs no docker
])
def test_the_dispatcher_exits_the_way_a_script_would_expect(args, expected):
    result = subprocess.run([str(TOPOMON), *args], capture_output=True, text=True,
                            cwd=str(ROOT), timeout=60)
    assert result.returncode == expected, result.stdout + result.stderr


@needs_bash
def test_version_names_the_checkout():
    result = subprocess.run([str(TOPOMON), "version"], capture_output=True,
                            text=True, cwd=str(ROOT), timeout=60)
    assert "commit" in result.stdout
    assert str(ROOT) in result.stdout


def test_install_hands_over_to_the_cli():
    """
    install.sh prepares the machine and then execs tools/topomon deploy. If it
    grew its own copy of "start the containers", the two would drift and only
    one of them would be the tested one.
    """
    text = INSTALL.read_text()
    assert 'exec "$TOPOMON_DIR/tools/topomon" deploy' in text
    assert "docker compose up" not in text
    # It is piped into bash, so it must not read from stdin, and it must only
    # act once the whole file has arrived (hence a single call at the end).
    assert text.rstrip().endswith('main "$@"')
    assert "read -r" not in text


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
