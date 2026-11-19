"""
What the HTTP surface promises: the two health probes, and what is *not* there.

The probes are not decoration. deploy/nginx.conf lets them through without a
password and `topomon health` decides whether a deployment is working from
them, so "ready" has to mean the database answered, and a failure has to be a
503 rather than a 200 with sad JSON in it - a load balancer reads the status
line, not the body.

The rest of this file guards removals. A stub endpoint that answers
plausibly is worse than no endpoint at all, and so is a CORS header that was
copied in before anyone asked what it allowed.
"""
import asyncio
import importlib

import pytest
from fastapi.middleware.cors import CORSMiddleware
from tests.conftest import authed_client

import app.main as main_module
from app.config import get_settings


def run(coro):
    return asyncio.run(coro)


async def _request(path, **kwargs):
    async with authed_client(main_module.app, "viewer") as client:
        return await client.get(path, **kwargs)


# -- the probes ------------------------------------------------------------


def test_liveness_answers_without_touching_anything():
    response = run(_request("/health"))
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readiness_says_the_database_answered():
    response = run(_request("/health/ready"))
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_readiness_fails_with_a_status_code_not_a_cheerful_body(monkeypatch):
    """
    The caller is a load balancer or `topomon health`, and both branch on the
    status line. A 200 saying {"database": "error"} reads as healthy to every
    probe ever written.
    """
    class Broken:
        def connect(self):
            raise RuntimeError("could not connect to 10.0.0.5:5432 as topomon")

    monkeypatch.setattr(main_module, "engine", Broken())
    response = run(_request("/health/ready"))

    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "unavailable", "database": "error"}
    # And the reason stays in the log: this endpoint is unauthenticated, and
    # the exception names the host, the port and the user it tried
    assert "10.0.0.5" not in response.text
    assert "topomon" not in response.text


# -- what was removed ------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/discovery/start",
                                  "/api/v1/discovery/status"])
def test_the_placeholder_discovery_job_is_gone(path):
    """
    These used to answer: /start slept two seconds and reported "discovered
    0, failed 0", which looks exactly like a network where nothing was found.
    Discovery happens in the collector; the API's share of it is /manual.
    """
    assert run(_request(path)).status_code == 404

    # Asked the same way, the endpoint that *does* exist answers 405 (it is a
    # POST) rather than 404 - so the 404 above is a missing route, not a
    # missing router.
    assert run(_request("/api/v1/discovery/manual")).status_code == 405


# -- CORS ------------------------------------------------------------------


def test_no_cross_origin_access_by_default():
    """
    The UI is served by this same process, so nothing legitimate needs a CORS
    header. What was here before said allow_origins=["*"] with
    allow_credentials=True - a combination browsers refuse, which is the only
    reason it was not a way for any open tab to read the device list.
    """
    assert get_settings().cors_origin_list() == []
    assert CORSMiddleware not in [m.cls for m in main_module.app.user_middleware]

    response = run(_request("/health", headers={"Origin": "http://evil.example"}))
    assert "access-control-allow-origin" not in response.headers


def test_an_origin_can_be_allowed_explicitly(monkeypatch):
    """The escape hatch still has to work, or someone will reach for ["*"]."""
    monkeypatch.setenv("APP_CORS_ORIGINS", "https://noc.example, https://noc.example")
    get_settings.cache_clear()
    try:
        assert get_settings().cors_origin_list() == ["https://noc.example"]
        reloaded = importlib.reload(main_module)
        assert CORSMiddleware in [m.cls for m in reloaded.app.user_middleware]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(main_module)

    assert CORSMiddleware not in [m.cls for m in main_module.app.user_middleware]
