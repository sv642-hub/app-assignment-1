"""JevResult: each answer validated on its own, and how the gateway uses them."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.router import JevAnswer, JevResult
from app_assignment_1.schemas import RoutingDecision

ROUTE_OK = {
    "choice": "complex",
    "confidence": 0.7,
    "probabilities": {"standard": 0.3, "complex": 0.7},
}
INTENT_OK = {
    "choice": "card_arrival",
    "confidence": 0.8,
    "probabilities": {"card_arrival": 0.8, "unknown": 0.2},
}
INVALID = {"error_type": "invalid_choice"}


def result(route: dict[str, Any] = ROUTE_OK, intent: dict[str, Any] = INTENT_OK):
    return {
        "route": route,
        "intent": intent,
        "mocked": False,
        "jev_model": "jev-1.14.0",
        "provider_request_id": "req_123",
        "input_tokens": 900,
        "output_tokens": 12,
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


class FixedRouter:
    def __init__(self, value: Any):
        self.value = value

    async def route(self, message: str) -> Any:
        return self.value


def chat(settings: Settings, value: Any, message: str = "card arrival") -> dict:
    with TestClient(create_app(settings, FixedRouter(value))) as client:
        response = client.post("/v1/chat", json={"message": message})
        assert response.status_code == 200
        return response.json()


@pytest.mark.parametrize("answer", [ROUTE_OK, INTENT_OK, INVALID])
def test_valid_answers(answer: dict[str, Any]) -> None:
    JevAnswer(**answer)


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({**INVALID, "choice": "complex"}, "invalid answer has no choice"),
        ({"choice": "complex", "confidence": 0.7}, "needs a choice"),
        ({}, "needs a choice"),
        ({**ROUTE_OK, "choice": "standard"}, "highest probability"),
        ({**ROUTE_OK, "choice": "other"}, "one of the options"),
        (
            {**ROUTE_OK, "probabilities": {"standard": 0.1, "complex": 0.1}},
            "add up to about 1",
        ),
        ({**ROUTE_OK, "confidence": 2}, "confidence"),
        ({"error_type": ""}, "error_type"),
        ({**ROUTE_OK, "reason": "x"}, "Extra inputs"),
    ],
)
def test_invalid_answers(answer: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        JevAnswer(**answer)


def test_route_answer_must_choose_standard_or_complex() -> None:
    route = {"choice": "fast", "confidence": 1, "probabilities": {"fast": 1.0}}
    with pytest.raises(ValidationError, match="standard or complex"):
        JevResult(**result(route=route))


def test_result_needs_the_call_details() -> None:
    values = result()
    del values["jev_model"]
    with pytest.raises(ValidationError, match="jev_model"):
        JevResult(**values)


def test_valid_route_is_used(settings: Settings) -> None:
    trace = chat(settings, JevResult(**result()))["trace"]
    assert trace["router"] == "mock_jev"
    assert trace["jev_choice"] == "complex"
    assert trace["route"] == "complex"
    assert trace["model"] == "qwen3-4b"
    assert trace["confidence"] == 0.7
    assert trace["fallback_reason"] is None


def test_router_may_return_a_plain_dict(settings: Settings) -> None:
    assert chat(settings, result())["trace"]["jev_choice"] == "complex"


def test_invalid_route_answer_uses_the_rule_router(settings: Settings, caplog) -> None:
    response = chat(settings, JevResult(**result(route=INVALID)))
    trace = response["trace"]
    assert trace["router"] == "rule_router"
    assert trace["jev_choice"] is None
    assert trace["confidence"] is None
    assert trace["route"] == "standard"  # a short message
    assert trace["fallback_reason"] == "jev_invalid_route"
    assert response["intent"] == "card_arrival"
    assert "invalid_choice" in caplog.text
    assert "card arrival" not in caplog.text


def test_invalid_intent_answer_keeps_the_jev_route(settings: Settings) -> None:
    trace = chat(settings, JevResult(**result(intent=INVALID)))["trace"]
    assert trace["router"] == "mock_jev"
    assert trace["jev_choice"] == "complex"
    assert trace["fallback_reason"] is None


def test_both_answers_invalid_uses_the_rule_router(settings: Settings) -> None:
    trace = chat(settings, JevResult(**result(route=INVALID, intent=INVALID)))
    assert trace["trace"]["fallback_reason"] == "jev_invalid_route"


@pytest.mark.parametrize(
    "value",
    [
        RoutingDecision(route="complex", confidence=0.7),  # the old return type
        {**result(), "route": {**ROUTE_OK, "choice": "standard"}},
        None,
    ],
)
def test_malformed_result_is_a_jev_error(settings: Settings, value: Any) -> None:
    trace = chat(settings, value)["trace"]
    assert trace["router"] == "rule_router"
    assert trace["fallback_reason"] == "jev_error"


def test_invalid_route_answer_is_counted_in_metrics(settings: Settings) -> None:
    router = FixedRouter(JevResult(**result(route=INVALID)))
    with TestClient(create_app(settings, router)) as client:
        client.post("/v1/chat", json={"message": "card arrival"})
        metrics = client.get("/metrics").text
    assert 'gateway_jev_fallbacks_total{reason="jev_invalid_route"} 1.0' in metrics
    assert 'gateway_jev_fallbacks_total{reason="jev_error"} 0.0' in metrics
