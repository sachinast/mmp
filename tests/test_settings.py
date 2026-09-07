"""Configuration fails at boot or not at all."""

import pytest
from mmp_core.settings import Settings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """These tests assert on the schema, not on the developer's shell."""
    for key in [k for k in list(__import__("os").environ) if k.startswith("MMP_")]:
        monkeypatch.delenv(key, raising=False)


BASE = {
    "_env_file": None,
    "database_url": "postgresql+asyncpg://u@127.0.0.1:5432/db",
    "redis_url": "redis://127.0.0.1:6379/0",
    "api_key_pepper": "a" * 64,
    "ip_hash_pepper": "b" * 64,
    "session_secret": "c" * 64,
}


def test_valid_settings_load():
    settings = Settings(**BASE)
    assert settings.environment == "dev"
    assert settings.asyncpg_dsn().startswith("postgresql://")


def test_missing_secret_is_a_boot_failure():
    with pytest.raises(ValidationError):
        Settings(**{k: v for k, v in BASE.items() if k != "api_key_pepper"})


def test_short_secret_rejected():
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "session_secret": "tooshort"})


def test_placeholder_secret_rejected():
    """The classic production incident: shipping the example value."""
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "api_key_pepper": "changeme" + "0" * 56})


def test_unknown_variable_rejected():
    """A typo'd setting name must not silently do nothing."""
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "databse_url": "oops"})
