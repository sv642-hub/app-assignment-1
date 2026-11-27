"""Public API checks for beta feedback #24 and positive reports #27–29."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.no_match import (
    GOODBYE_REPLY,
    GREETING_REPLY,
    THANKS_REPLY,
    UNKNOWN_REPLY,
    no_match_reply,
)


@pytest.fixture
def client(tmp_path):
    configs = json.loads(Path("config/backends.json").read_text())
    for config in configs:
        config["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(configs))
    with TestClient(create_app(Settings(_env_file=None, backends_path=path))) as client:
        yield client


@pytest.mark.parametrize(
    "message",
    [
        "Can you help me with it?",
        "Can you help me with my bank card?",
        "I need some help",
        "Something is wrong with my account",
    ],
)
def test_vague_message_asks_for_problem_without_guessing_policy(client, message):
    response = client.post("/v1/chat", json={"message": message})
    assert response.status_code == 200
    result = response.json()
    # Check what the customer needs to do next, not the backend's text constant.
    assert "describe" in result["reply"].lower()
    assert "banking problem" in result["reply"].lower()
    assert "?" in result["reply"]
    assert result["intent"] == "unknown"
    assert result["policy_facts"] == []
    assert result["trace"]["reply_source"] == "no_match"
    assert result["trace"]["format_check"] == "passed"
    assert client.get(f"/v1/requests/{result['id']}").json() == result


def test_clarification_then_specific_request_is_answered(client):
    # More than the intervention threshold: uncertainty is not a format failure.
    for _ in range(6):
        client.post("/v1/chat", json={"message": "Can you help me with it?"})
    result = client.post("/v1/chat", json={"message": "My card has not arrived"}).json()
    assert result["intent"] == "card_arrival"
    assert result["policy_facts"]
    assert all(fact in result["reply"] for fact in result["policy_facts"])
    assert result["trace"]["reply_source"] == "policy_template"
    assert result["trace"]["fallback_reason"] is None
    assert client.get("/v1/health").json()["status"] == "ok"


def test_weather_stays_out_of_scope(client):
    response = client.post("/v1/chat", json={"message": "what's the weather today?"})
    assert response.status_code == 200
    result = response.json()
    assert result["intent"] == "unknown"
    assert result["policy_facts"] == []
    assert "could not match" in result["reply"]
    assert "fictional bank demo" in result["reply"]
    assert result["trace"]["reply_source"] == "no_match"


def test_documented_chat_endpoint_returns_reply_and_trace(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "200" in schema["paths"]["/v1/chat"]["post"]["responses"]
    response = client.post("/v1/chat", json={"message": "My card has not arrived"})
    assert response.status_code == 200
    assert response.json()["reply"]
    assert response.json()["trace"]["format_check"] == "passed"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("hi", GREETING_REPLY),
        ("Hello there!", GREETING_REPLY),
        ("Good morning", GREETING_REPLY),
        ("thanks", THANKS_REPLY),
        ("Thank you so much!", THANKS_REPLY),
        ("bye", GOODBYE_REPLY),
        ("Goodbye :)", GOODBYE_REPLY),
    ],
)
def test_greeting_thanks_and_bye_get_a_short_fixed_reply(client, message, expected):
    result = client.post("/v1/chat", json={"message": message}).json()
    assert result["intent"] == "unknown"
    assert result["policy_facts"] == []
    assert result["reply"] == expected
    assert result["trace"]["reply_source"] == "no_match"


def test_other_no_match_messages_list_topics(client):
    result = client.post(
        "/v1/chat", json={"message": "tell me a joke about penguins"}
    ).json()
    assert result["reply"] == UNKNOWN_REPLY
    assert "exchange rates" in result["reply"]
    assert "penguins" not in result["reply"]


@pytest.mark.parametrize(
    "message",
    ["hi, where is my card", "thanks but what is the weather", "bye and tell a joke"],
)
def test_a_greeting_with_more_in_it_is_not_a_pure_greeting(message):
    assert no_match_reply(message) == UNKNOWN_REPLY
