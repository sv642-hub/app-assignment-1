"""API tests: /v1/chat schema and validation, request lookup, health, docs."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smart_gateway import __version__, api
from smart_gateway.client import send_chat
from smart_gateway.schemas import ChatResponse

TRACE_KEYS = {
    "router",
    "route",
    "confidence",
    "model",
    "latency_ms",
    "format_check",
    "fallback_reason",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    # Same backends as config/backends.json, minus the simulated latency.
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends["backends"]:
        backend["latency_ms"] = 0
    fast_backends = tmp_path / "backends.json"
    fast_backends.write_text(json.dumps(backends))
    monkeypatch.setenv("APP_BACKENDS_PATH", str(fast_backends))

    with TestClient(api.app) as test_client:
        yield test_client


def test_chat_response_schema(client: TestClient) -> None:
    response = client.post("/v1/chat", json={"message": "I lost my card"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "request_id",
        "created_at",
        "message",
        "intent",
        "reply",
        "policy_card",
        "trace",
    }
    assert set(body["trace"]) == TRACE_KEYS
    assert set(body["policy_card"]) == {"intent", "title", "facts"}
    parsed = ChatResponse.model_validate(body)
    assert parsed.message == "I lost my card"
    assert parsed.intent == "lost_or_stolen_card"
    assert parsed.trace.route == "complex"
    assert parsed.trace.model == "qwen3-4b"
    assert parsed.trace.format_check == "pass"


def test_chat_unknown_message_uses_rule_router(client: TestClient) -> None:
    response = client.post("/v1/chat", json={"message": "What's the weather?"})

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "unknown"
    assert body["policy_card"] is None
    assert body["trace"]["router"] == "rule-router"
    assert body["trace"]["model"] is None


def test_chat_strips_whitespace(client: TestClient) -> None:
    response = client.post("/v1/chat", json={"message": "  refund  "})

    assert response.json()["message"] == "refund"


@pytest.mark.parametrize(
    "payload",
    [{}, {"message": ""}, {"message": "   "}, {"message": "x" * 2001}, {"text": "hi"}],
)
def test_chat_rejects_invalid_input(client: TestClient, payload: dict) -> None:
    response = client.post("/v1/chat", json=payload)

    assert response.status_code == 422


def test_get_request_returns_stored_response(client: TestClient) -> None:
    created = client.post("/v1/chat", json={"message": "kyc"}).json()

    response = client.get(f"/v1/requests/{created['request_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_request_returns_404(client: TestClient) -> None:
    response = client.get("/v1/requests/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Request not found."}


def test_health_ok(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "environment": api.settings.environment,
        "router": "mock-jev",
        "intents": 10,
        "backends": ["qwen3-1.7b", "qwen3-4b"],
    }


def test_endpoints_need_a_started_gateway() -> None:
    # Without the context manager the lifespan never runs.
    client = TestClient(api.app)

    response = client.post("/v1/chat", json={"message": "hello"})

    assert response.status_code == 503


def test_docs_and_schema_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/v1/chat", "/v1/requests/{request_id}", "/v1/health"} <= paths.keys()
    assert {"200", "422", "503"} <= paths["/v1/chat"]["post"]["responses"].keys()


def test_playground_client_round_trip(client: TestClient) -> None:
    # TestClient is an httpx.Client, so this runs the Playground's HTTP call
    # against the real app.
    result = send_chat(client, "My top up failed")

    assert result.intent == "top_up_failed"
    assert result.trace.model == "qwen3-1.7b"
