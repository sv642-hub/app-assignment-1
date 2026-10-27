"""Playground page tests with Streamlit's AppTest, against the real gateway app."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from smart_gateway import api, client

PAGE = str(Path(__file__).resolve().parents[1] / "src/smart_gateway/playground.py")


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    # Point the page's HTTP client at the app in-process instead of a server.
    monkeypatch.setattr(client, "make_client", lambda settings: TestClient(api.app))
    # The first run imports Streamlit and the app, which can take a while on CI.
    return AppTest.from_file(PAGE, default_timeout=30).run()


def test_example_button_shows_intent_reply_and_trace(page: AppTest) -> None:
    page.button(key="example-1").click().run()

    assert [m.value for m in page.metric] == ["lost_or_stolen_card", "complex"]
    assert "Freeze the card" in page.info[0].value
    trace = page.table[0].value
    assert dict(zip(trace["Field"], trace["Value"], strict=True))["Model"] == "qwen3-4b"


def test_typed_message_unknown_uses_rule_router(page: AppTest) -> None:
    page.text_area(key="message").input("Tell me a joke")
    page.button[-1].click().run()

    assert page.metric[0].value == "unknown"
    trace = page.table[0].value
    fields = dict(zip(trace["Field"], trace["Value"], strict=True))
    assert fields["Model"] == "none (rule router)"
    assert fields["Fallback reason"] == "no_intent_match"


def test_empty_message_warns(page: AppTest) -> None:
    page.button[-1].click().run()

    assert page.warning[0].value == "Type a message first."
