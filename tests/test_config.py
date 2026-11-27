"""Settings precedence, caching and invalid configuration."""

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app_assignment_1.config import Settings, get_settings
from app_assignment_1.real_backends import create_vllm_http_client, vllm_base_url


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


# Missing URLs, a missing key and "no Jev key needed" are covered in
# tests/test_labels.py (test_real_qwen_needs_both_urls_and_a_key and
# test_real_qwen_does_not_need_a_jev_key).
REAL_QWEN = {
    "provider_mode": "real",
    "vllm_small_base_url": "http://small.test:8001",
    "vllm_large_base_url": "http://large.test:8002",
}


@pytest.mark.parametrize("key", ["", "   ", "\t\r\n"])
def test_real_provider_refuses_a_blank_vllm_key(key):
    with pytest.raises(ValidationError, match="needs APP_VLLM_API_KEY"):
        Settings(_env_file=None, vllm_api_key=key, **REAL_QWEN)


@pytest.mark.parametrize(
    "key",
    [
        " test-vllm-key",
        "test-vllm-key ",
        "test-vllm-key\r",
        "test-vllm-key\r\n",
        "\ttest-vllm-key\n",
    ],
)
def test_real_provider_stores_the_vllm_key_stripped(key):
    # A CRLF key file read with $(cat file) keeps its "\r" (#122 review).
    settings = Settings(_env_file=None, vllm_api_key=key, **REAL_QWEN)
    assert settings.vllm_api_key is not None
    assert settings.vllm_api_key.get_secret_value() == "test-vllm-key"


@pytest.mark.parametrize(
    "key", ["test vllm key", "test\tkey", "k\x00ey", "kéy-unicode"]
)
def test_real_provider_refuses_a_vllm_key_that_cannot_be_sent(key):
    with pytest.raises(ValidationError, match="printable ASCII"):
        Settings(_env_file=None, vllm_api_key=key, **REAL_QWEN)


def test_the_vllm_client_sends_the_stripped_key():
    sent: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    settings = Settings(_env_file=None, vllm_api_key=" test-vllm-key\r", **REAL_QWEN)

    async def call() -> None:
        url = vllm_base_url(settings, "qwen3-1.7b")
        transport = httpx.MockTransport(server)
        async with create_vllm_http_client(settings, url, transport) as client:
            await client.post("/v1/chat/completions", json={})

    asyncio.run(call())
    assert sent == ["Bearer test-vllm-key"]
