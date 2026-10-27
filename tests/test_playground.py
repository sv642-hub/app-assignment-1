"""Exercise the actual Streamlit page with HTTP responses at its boundary."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings, get_settings

PAGE = Path(__file__).resolve().parents[1] / "src/app_assignment_1/playground.py"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setenv("APP_GATEWAY_URL", "http://gateway.test:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_example_submission_renders_reply_and_trace(monkeypatch):
    with TestClient(create_app(Settings(_env_file=None))) as client:
        payload = client.post(
            "/v1/chat", json={"message": "My card has not arrived"}
        ).json()
    calls = []

    def post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    page = AppTest.from_file(PAGE, default_timeout=15).run()
    assert not page.exception
    page.button[0].click().run()
    assert page.text_area[0].value == "My card has not arrived"
    page.button[-1].click().run()
    assert not page.exception
    assert calls == [
        ("http://gateway.test:8000/v1/chat", {"message": "My card has not arrived"}, 15)
    ]
    assert page.code[0].value == "card_arrival"
    assert payload["reply"] in [m.value for m in page.markdown]
    assert "qwen3-1.7b" in page.table[0].value["Value"].tolist()
    assert "passed" in page.table[0].value["Value"].tolist()


def test_blank_input_does_not_call_gateway(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Blank input should not call the gateway")

    monkeypatch.setattr(httpx, "post", unexpected)
    page = AppTest.from_file(PAGE, default_timeout=15).run()
    page.text_area[0].input("   ")
    page.button[-1].click().run()
    assert page.warning[0].value == "Enter a banking message first."
    assert not page.exception


@pytest.mark.parametrize("kind", ["unavailable", "network", "invalid"])
def test_gateway_errors_are_visible(monkeypatch, kind):
    def post(url, **kwargs):
        if kind == "network":
            raise httpx.ConnectError("offline")
        return httpx.Response(
            503 if kind == "unavailable" else 200,
            json={},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    page = AppTest.from_file(PAGE, default_timeout=15).run()
    page.text_area[0].input("I lost my card")
    page.button[-1].click().run()
    assert len(page.error) == 1
    assert not page.exception
    assert len(page.table) == 0
