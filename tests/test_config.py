"""Settings precedence, caching and invalid configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app_assignment_1.config import Settings, get_settings


def test_paths_from_env_override_file(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("APP_INTENTS_PATH=file.json\nAPP_REQUEST_STORE_LIMIT=5\n")
    monkeypatch.setenv("APP_INTENTS_PATH", "override.json")
    settings = Settings(_env_file=env)
    assert settings.intents_path == Path("override.json")
    assert settings.request_store_limit == 5


def test_cache_requires_clear(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("APP_REQUEST_STORE_LIMIT", "4")
        first = get_settings()
        monkeypatch.setenv("APP_REQUEST_STORE_LIMIT", "8")
        assert get_settings() is first
        assert get_settings().request_store_limit == 4
        get_settings.cache_clear()
        assert get_settings().request_store_limit == 8
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_store_limit", 0),
        ("environment", "typo"),
        ("jev_timeout_seconds", 0),
        ("rule_router_complex_min_words", 0),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
