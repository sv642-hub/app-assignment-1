"""The SSE route: progress while a model is slow, then one result or error."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.service import BackendUnavailable
from app_assignment_1.streaming import ERROR_TEXT, sse


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(
        _env_file=None,
        backends_path=path,
        rate_limit_per_minute=0,
        stream_heartbeat_seconds=0.05,
    )


def events(response) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    out = []
    for frame in response.text.strip().split("\n\n"):
        name, data = frame.split("\n")
        assert name.startswith("event: ") and data.startswith("data: ")
        out.append((name[7:], json.loads(data[6:])))
    return out


def test_frame_format() -> None:
    assert sse("progress", {"a": 1}) == 'event: progress\ndata: {"a":1}\n\n'


def test_a_normal_answer_streams_stages_then_the_result(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/stream", json={"message": "My card has not arrived"}
        )
        plain = client.post("/v1/chat", json={"message": "My card has not arrived"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert "x-request-id" in response.headers
    seen = events(response)
    names = [name for name, _ in seen]
    assert names[-1] == "result"
    assert names.count("result") == 1 and "error" not in names
    stages = [d["stage"] for n, d in seen if n == "progress"]
    assert stages[:2] == ["routing", "generating"]
    generating = next(d for n, d in seen if d.get("stage") == "generating")
    assert generating["model"] == "qwen3-1.7b"
    assert all(d["message"] for n, d in seen if n == "progress")
    result = seen[-1][1]
    assert result["intent"] == plain.json()["intent"] == "card_arrival"
    assert result["trace"]["model"] == "qwen3-1.7b"


def test_a_slow_model_sends_waiting_heartbeats(settings) -> None:
    with TestClient(create_app(settings)) as client:
        gateway = client.app.state.gateway
        original = gateway.backends[0].generate

        async def slow(message):
            await asyncio.sleep(0.4)
            return await original(message)

        gateway.backends[0].generate = slow
        seen = events(client.post("/v1/chat/stream", json={"message": "card arrival"}))
    waiting = [d for n, d in seen if d.get("stage") == "waiting"]
    assert len(waiting) >= 3
    assert waiting[-1]["elapsed_seconds"] >= waiting[0]["elapsed_seconds"]
    assert "longer than usual" in waiting[0]["message"]
    assert seen[-1][0] == "result"


def test_no_model_is_an_error_event_with_a_clear_message(settings) -> None:
    backends = json.loads(settings.backends_path.read_text())
    for backend in backends:
        backend["available"] = False
    settings.backends_path.write_text(json.dumps(backends))
    with TestClient(create_app(settings)) as client:
        seen = events(client.post("/v1/chat/stream", json={"message": "card arrival"}))
    name, data = seen[-1]
    assert name == "error"
    assert data == {
        "status": 503,
        "code": "no_model_available",
        "message": ERROR_TEXT["no_model_available"],
    }
    assert [n for n, _ in seen].count("error") == 1


def test_the_deadline_is_its_own_error(settings) -> None:
    settings.request_deadline_seconds = 1.0
    settings.alternate_reserve_seconds = 0.5
    settings.jev_timeout_seconds = 0.01

    with TestClient(create_app(settings)) as client:
        gateway = client.app.state.gateway

        async def hang(message):
            await asyncio.sleep(30)

        for backend in gateway.backends:
            backend.generate = hang
        seen = events(client.post("/v1/chat/stream", json={"message": "card arrival"}))
    name, data = seen[-1]
    assert (name, data["code"], data["status"]) == ("error", "deadline_exceeded", 503)
    assert "too long" in data["message"]


def test_an_unexpected_failure_is_a_500_event_without_details(settings, capsys) -> None:
    with TestClient(create_app(settings)) as client:

        async def boom(message, progress=None):
            raise RuntimeError("secret internal detail 4111")

        client.app.state.gateway.chat = boom
        seen = events(client.post("/v1/chat/stream", json={"message": "hi"}))
    name, data = seen[-1]
    assert (name, data["status"], data["code"]) == ("error", 500, "internal_error")
    assert "4111" not in json.dumps(data)
    assert "4111" not in capsys.readouterr().err
    assert "X-Request-ID" in data["message"]


def test_bad_input_is_a_plain_422_before_any_stream(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat/stream", json={"message": ""})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"


def test_the_stream_route_is_documented(settings) -> None:
    with TestClient(create_app(settings)) as client:
        spec = client.get("/openapi.json").json()
    post = spec["paths"]["/v1/chat/stream"]["post"]
    assert "text/event-stream" in post["responses"]["200"]["content"]
    assert {"413", "422", "429"} <= set(post["responses"])


def test_unavailable_error_maps_to_503() -> None:
    from app_assignment_1.streaming import error_event

    assert error_event(BackendUnavailable()) == (503, "no_model_available")
    assert error_event(KeyError("x")) == (500, "internal_error")
