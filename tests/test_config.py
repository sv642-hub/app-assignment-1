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


def test_vllm_urls_default_to_unset():
    settings = Settings(_env_file=None)
    assert settings.vllm_small_base_url is None
    assert settings.vllm_large_base_url is None


def test_vllm_urls_from_env(monkeypatch):
    monkeypatch.setenv("APP_VLLM_SMALL_BASE_URL", "http://10.0.12.34:8001")
    monkeypatch.setenv("APP_VLLM_LARGE_BASE_URL", "http://10.0.12.34:8002")
    settings = Settings(_env_file=None)
    assert str(settings.vllm_small_base_url) == "http://10.0.12.34:8001/"
    assert settings.vllm_large_base_url is not None
    assert settings.vllm_large_base_url.port == 8002


def test_vllm_url_must_be_a_url():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vllm_small_base_url="not a url")


def test_vllm_api_key_is_secret(monkeypatch):
    monkeypatch.setenv("APP_VLLM_API_KEY", "k" * 64)
    settings = Settings(_env_file=None)
    assert settings.vllm_api_key is not None
    assert settings.vllm_api_key.get_secret_value() == "k" * 64
    assert "k" * 64 not in repr(settings)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("vllm_small_base_url", None, "APP_VLLM_SMALL_BASE_URL"),
        ("vllm_large_base_url", None, "APP_VLLM_LARGE_BASE_URL"),
        ("vllm_api_key", None, "APP_VLLM_API_KEY"),
        ("vllm_api_key", "", "APP_VLLM_API_KEY"),
        ("vllm_api_key", "   ", "APP_VLLM_API_KEY"),
    ],
)
def test_real_provider_requires_vllm_settings(field, value, expected):
    options = {
        "provider_mode": "real",
        "jev_mode": "mock",
        "jev_api_key": None,
        "vllm_small_base_url": "http://small.test:8001",
        "vllm_large_base_url": "http://large.test:8002",
        "vllm_api_key": "test-vllm-key",
    }
    options[field] = value

    with pytest.raises(ValidationError, match=expected):
        Settings(_env_file=None, **options)


def test_real_provider_with_mock_jev_needs_no_jev_key():
    settings = Settings(
        _env_file=None,
        provider_mode="real",
        jev_mode="mock",
        jev_api_key=None,
        vllm_small_base_url="http://small.test:8001",
        vllm_large_base_url="http://large.test:8002",
        vllm_api_key="test-vllm-key",
    )

    assert settings.provider_mode == "real"
    assert settings.jev_mode == "mock"
    assert settings.jev_api_key is None
    assert settings.vllm_small_base_url.host == "small.test"
    assert settings.vllm_large_base_url.host == "large.test"
