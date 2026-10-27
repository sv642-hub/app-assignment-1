"""Contract, fallback and responsiveness tests with no external services."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.schemas import ChatResponse


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


@pytest.mark.parametrize(
    "message,intent,model",
    [
        ("My card has not arrived", "card_arrival", "qwen3-1.7b"),
        ("I lost my card", "lost_or_stolen_card", "qwen3-4b"),
    ],
)
def test_chat_contract_and_history(settings, message, intent, model):
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat", json={"message": message})
        assert response.status_code == 200
        result = ChatResponse.model_validate(response.json())
        assert result.intent == intent
        assert result.trace.model == model
        assert result.trace.mocked is True
        assert result.trace.format_check == "passed"
        assert result.trace.jev_choice == result.trace.route
        assert all(fact in result.reply for fact in result.policy_facts)
        assert client.get(f"/v1/requests/{result.id}").json() == response.json()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "a" * 2001},
        {"message": 123},
        {"message": "test", "extra": True},
    ],
)
def test_invalid_inputs(settings, payload):
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/chat", json=payload).status_code == 422


def test_unknown_message_still_calls_a_backend(settings):
    with TestClient(create_app(settings)) as client:
        result = client.post("/v1/chat", json={"message": "weather on Mars"}).json()
        assert result["intent"] == "unknown"
        assert result["policy_facts"] == []
        trace = result["trace"]
        assert trace["router"] == "mock_jev"
        assert trace["jev_choice"] == "standard"
        assert trace["model"] == "qwen3-1.7b"
        assert trace["reply_source"] == "no_match"
        assert trace["fallback_reason"] is None
        assert "rephrase" in result["reply"]


@pytest.mark.parametrize(
    "message,route,model",
    [
        ("I lost my card", "standard", "qwen3-1.7b"),
        (" ".join(["lost card"] * 10), "complex", "qwen3-4b"),
    ],
)
def test_jev_failure_uses_rule_router_and_a_backend(settings, message, route, model):
    class OfflineRouter:
        async def route(self, message):
            raise ConnectionError

    with TestClient(create_app(settings, OfflineRouter())) as client:
        result = client.post("/v1/chat", json={"message": message}).json()
        trace = result["trace"]
        assert result["intent"] == "lost_or_stolen_card"
        assert trace["router"] == "rule_router"
        assert trace["jev_choice"] is None
        assert trace["confidence"] is None
        assert (trace["route"], trace["model"]) == (route, model)
        assert trace["reply_source"] == "policy_template"
        assert trace["fallback_reason"] == "jev_error"


def test_slow_jev_times_out_to_rule_router(settings):
    class SlowRouter:
        async def route(self, message):
            await asyncio.sleep(5)

    settings.jev_timeout_seconds = 0.05
    with TestClient(create_app(settings, SlowRouter())) as client:
        result = client.post("/v1/chat", json={"message": "card arrival"}).json()
        assert result["intent"] == "card_arrival"
        assert result["trace"]["router"] == "rule_router"
        assert result["trace"]["fallback_reason"] == "jev_timeout"
        assert result["trace"]["latency_ms"] < 1000


@pytest.mark.parametrize("both_down", [False, True])
def test_backend_fallback(settings, both_down):
    backends = json.loads(settings.backends_path.read_text())
    backends[0]["available"] = False
    backends[1]["available"] = not both_down
    settings.backends_path.write_text(json.dumps(backends))
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat", json={"message": "card arrival"})
        if both_down:
            assert response.status_code == 503
            assert client.get("/v1/health").json()["status"] == "degraded"
        else:
            assert response.status_code == 200
            trace = response.json()["trace"]
            assert trace["model"] == "qwen3-4b"
            assert trace["jev_choice"] == "standard"
            assert trace["route"] == "complex"
            assert trace["fallback_reason"] == "selected_backend_unavailable"


def test_history_is_bounded_and_missing_ids(settings):
    settings.request_store_limit = 1
    with TestClient(create_app(settings)) as client:
        first = client.post("/v1/chat", json={"message": "card arrival"}).json()
        second = client.post("/v1/chat", json={"message": "lost card"}).json()
        assert client.get(f"/v1/requests/{first['id']}").status_code == 404
        assert client.get(f"/v1/requests/{second['id']}").status_code == 200
        assert client.get(f"/v1/requests/{uuid4()}").status_code == 404
        assert client.get("/v1/requests/not-a-uuid").status_code == 422


def test_docs_and_health(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/health").json()["mode"] == "mock"
        assert client.get("/docs").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert "/v1/chat" in paths
        assert "503" in paths["/v1/chat"]["post"]["responses"]


def test_bad_config_fails_startup(settings, tmp_path):
    bad = tmp_path / "cards.json"
    bad.write_text("{}")
    settings.policy_cards_path = bad
    with pytest.raises(ValueError, match="Policy cards"):
        with TestClient(create_app(settings)):
            pass


def test_health_remains_responsive(settings):
    async def run():
        backends = json.loads(settings.backends_path.read_text())
        backends[0]["latency_ms"] = 400
        settings.backends_path.write_text(json.dumps(backends))
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                pending = asyncio.create_task(
                    client.post("/v1/chat", json={"message": "card arrival"})
                )
                await asyncio.sleep(0.05)
                health = await client.get("/v1/health")
                assert health.status_code == 200
                assert not pending.done()
                assert (await pending).status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize("jev_down", [False, True])
def test_no_backend_means_503_for_every_message(settings, jev_down):
    class OfflineRouter:
        async def route(self, message):
            raise ConnectionError

    backends = json.loads(settings.backends_path.read_text())
    for backend in backends:
        backend["available"] = False
    settings.backends_path.write_text(json.dumps(backends))
    router = OfflineRouter() if jev_down else None
    with TestClient(create_app(settings, router)) as client:
        for message in ["weather on Mars", "card arrival"]:
            response = client.post("/v1/chat", json={"message": message})
            assert response.status_code == 503


def test_jev_failure_and_backend_fallback_are_both_traced(settings):
    class OfflineRouter:
        async def route(self, message):
            raise ConnectionError

    backends = json.loads(settings.backends_path.read_text())
    backends[0]["available"] = False
    settings.backends_path.write_text(json.dumps(backends))
    with TestClient(create_app(settings, OfflineRouter())) as client:
        trace = client.post("/v1/chat", json={"message": "card arrival"}).json()[
            "trace"
        ]
        assert trace["model"] == "qwen3-4b"
        assert trace["fallback_reason"] == "jev_error, selected_backend_unavailable"


def test_health_has_documented_response_model(settings):
    from app_assignment_1.schemas import HealthResponse

    with TestClient(create_app(settings)) as client:
        health = HealthResponse.model_validate(client.get("/v1/health").json())
        assert health.status == "ok"
        schema = client.get("/openapi.json").json()
        response = schema["paths"]["/v1/health"]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(
            "/HealthResponse"
        )
