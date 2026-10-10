"""
Settings that have to mean something.

APP_LOG_LEVEL was documented and ignored for the whole life of the project
(both entry points hardcoded INFO), which made the DEBUG lines the
troubleshooting guide points at unreachable. These keep it honest.
"""
import logging

import pytest

from app.config import get_settings, log_level


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("value,expected", [
    (None, logging.INFO),
    ("INFO", logging.INFO),
    ("DEBUG", logging.DEBUG),
    ("debug", logging.DEBUG),
    (" debug ", logging.DEBUG),
    ("WARNING", logging.WARNING),
    ("warn", logging.WARNING),
    ("ERROR", logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
    ("FATAL", logging.CRITICAL),
])
def test_log_level_reads_the_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("APP_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("APP_LOG_LEVEL", value)
    get_settings.cache_clear()
    assert log_level() == expected


@pytest.mark.parametrize("value", [
    "banana", "", "25",
    # These name real attributes of the logging module, so a getattr-based
    # implementation would hand basicConfig a string or a function
    "basic_format", "root", "warn_", "handlers",
])
def test_an_unusable_level_falls_back_to_info(monkeypatch, value):
    monkeypatch.setenv("APP_LOG_LEVEL", value)
    get_settings.cache_clear()
    assert log_level() == logging.INFO


def test_the_defaults_are_the_documented_ones(monkeypatch):
    """
    The damping defaults have to reproduce pre-damping behaviour, because an
    upgrade must not change when somebody gets paged.
    """
    for name in ("ALERT_OFFLINE_CONFIRM_CYCLES", "ALERT_LINK_FLAP_TRANSITIONS",
                 "ALERT_LINK_FLAP_WINDOW", "ALERT_TRAP_REQUIRES_POLL",
                 "COLLECTOR_SHARDS", "COLLECTOR_SHARD_ID"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.alert_offline_confirm_cycles == 1
    assert settings.alert_link_flap_transitions == 0
    assert settings.alert_link_flap_window == 600
    assert settings.alert_trap_requires_poll is False
    assert settings.collector_shards == 1
    assert settings.collector_shard_id == 0
