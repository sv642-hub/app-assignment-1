"""The real Jev client against a fake TypeSafe server (httpx.MockTransport)."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.jev import (
    ROUTE_CRITERIA,
    JevCallError,
    JevTimeout,
    TypeSafeJevRouter,
    build_questions,
    create_jev_http_client,
)
from app_assignment_1.labels import load_labels
from app_assignment_1.records import ROUTE_OPTIONS
from app_assignment_1.router import JevResult

KEY = "tsk-Hunter2"
MESSAGE = "my card has not arrived"
LABELS = load_labels(Path("config/labels.json"))
INTENT_OPTIONS = [item.label for item in LABELS.items]

Handler = Callable[[httpx.Request], httpx.Response]


def intent_answer(choice: str = "card_arrival", top: float = 0.9) -> dict[str, Any]:
    rest = (1 - top) / (len(INTENT_OPTIONS) - 1)
    return {
        "type": "choice",
        "choice": choice,
        "confidence": 0.8,
        "probabilities": {
            label: top if label == choice else rest for label in INTENT_OPTIONS
        },
    }


ROUTE_ANSWER = {
    "type": "choice",
    "choice": "standard",
    "confidence": 0.6,
    "probabilities": {"standard": 0.75, "complex": 0.25},
}


def body(**answers: Any) -> dict[str, Any]:
    return {
        "model": "jev-1.14.0",
        "answers": {"route": ROUTE_ANSWER, "intent": intent_answer(), **answers},
        "usage": {"input_tokens": 1500, "output_tokens": 40},
    }


def reply(payload: Any = None, status: int = 200, **headers: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json=body() if payload is None else payload, headers=headers
        )

    return handler


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, jev_api_key=KEY, **overrides)


def make_router(handler: Handler) -> TypeSafeJevRouter:
    client = create_jev_http_client(settings(), httpx.MockTransport(handler))
    return TypeSafeJevRouter(client, "jev-1.14.0", LABELS)


def route(handler: Handler, message: str = MESSAGE) -> JevResult:
    return asyncio.run(make_router(handler).route(message))


def call_error(handler: Handler) -> JevCallError:
    with pytest.raises(JevCallError) as caught:
        route(handler)
    return caught.value


def test_request_asks_both_questions_in_one_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body())

    route(handler)
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    sent = json.loads(request.content)
    assert sent["model"] == "jev-1.14.0"
    assert sent["state"] == MESSAGE
    assert set(sent["questions"]) == {"route", "intent"}
    assert sent["questions"]["route"]["type"] == "choice"
    assert sent["questions"]["route"]["criteria"] == ROUTE_CRITERIA
    intent = sent["questions"]["intent"]
    assert intent["type"] == "choice"
    # All 78 labels, in the label file's order, with their descriptions.
    assert list(intent["criteria"]) == INTENT_OPTIONS
    assert len(intent["criteria"]) == 78
    assert intent["criteria"] == {item.label: item.description for item in LABELS.items}


def test_route_criteria_are_the_route_options() -> None:
    assert set(ROUTE_CRITERIA) == ROUTE_OPTIONS
    assert set(build_questions(LABELS)["route"]["criteria"]) == ROUTE_OPTIONS


def test_valid_reply_gives_both_answers_and_call_details() -> None:
    result = route(reply(**{"x-typesafe-request-id": "req_abc"}))
    assert result.route.choice == "standard"
    assert result.route.confidence == 0.6
    assert result.route.probabilities == {"standard": 0.75, "complex": 0.25}
    assert result.intent.choice == "card_arrival"
    assert result.intent.error_type is None
    assert result.mocked is False
    assert result.jev_model == "jev-1.14.0"
    assert result.provider_request_id == "req_abc"
    assert (result.input_tokens, result.output_tokens) == (1500, 40)


def test_model_comes_from_the_response_not_the_request() -> None:
    result = route(reply({**body(), "model": "jev-1.14.1"}))
    assert result.jev_model == "jev-1.14.1"


def test_request_id_is_optional() -> None:
    assert route(reply()).provider_request_id is None
    assert route(reply(**{"x-typesafe-request-id": ""})).provider_request_id is None


def test_extra_fields_from_the_provider_are_ignored() -> None:
    answer = {**ROUTE_ANSWER, "rationale": "new field"}
    result = route(reply({**body(route=answer), "latency_ms": 120}))
    assert result.route.choice == "standard"


def test_tied_top_options_are_accepted() -> None:
    tied = {
        **ROUTE_ANSWER,
        "confidence": 0.0,
        "probabilities": {"standard": 0.5, "complex": 0.5},
    }
    assert route(reply(body(route=tied))).route.choice == "standard"


def test_integer_probabilities_are_accepted() -> None:
    certain = {
        **ROUTE_ANSWER,
        "confidence": 1,
        "probabilities": {"standard": 1, "complex": 0},
    }
    result = route(reply(body(route=certain)))
    assert result.route.probabilities == {"standard": 1.0, "complex": 0.0}


BAD_ANSWERS = [
    (None, "missing_answer"),
    ("standard", "wrong_type"),
    ({**ROUTE_ANSWER, "type": "noul"}, "wrong_type"),
    ({**ROUTE_ANSWER, "choice": 1}, "malformed_answer"),
    ({**ROUTE_ANSWER, "confidence": "high"}, "malformed_answer"),
    ({**ROUTE_ANSWER, "confidence": True}, "malformed_answer"),
    ({**ROUTE_ANSWER, "probabilities": [0.75, 0.25]}, "malformed_answer"),
    (
        {**ROUTE_ANSWER, "probabilities": {"standard": "0.75", "complex": 0.25}},
        "malformed_answer",
    ),
    ({**ROUTE_ANSWER, "probabilities": {"standard": 1.0}}, "wrong_options"),
    (
        {**ROUTE_ANSWER, "probabilities": {"standard": 0.7, "complex": 0.2, "x": 0.1}},
        "wrong_options",
    ),
    ({**ROUTE_ANSWER, "confidence": 1.5}, "invalid_confidence"),
    ({**ROUTE_ANSWER, "confidence": -0.1}, "invalid_confidence"),
    ({**ROUTE_ANSWER, "choice": "complex"}, "invalid_probabilities"),
    ({**ROUTE_ANSWER, "choice": "fast"}, "invalid_probabilities"),
    (
        {**ROUTE_ANSWER, "probabilities": {"standard": 0.5, "complex": 0.3}},
        "invalid_probabilities",
    ),
    (
        {**ROUTE_ANSWER, "probabilities": {"standard": 1.2, "complex": -0.2}},
        "invalid_probabilities",
    ),
]


@pytest.mark.parametrize(("answer", "error_type"), BAD_ANSWERS)
def test_bad_route_answer_keeps_the_intent(answer: Any, error_type: str) -> None:
    payload = body()
    if answer is None:
        del payload["answers"]["route"]
    else:
        payload["answers"]["route"] = answer
    result = route(reply(payload))
    assert result.route.error_type == error_type
    assert result.route.choice is None
    assert result.intent.choice == "card_arrival"


def test_nan_confidence_is_invalid() -> None:
    raw = json.dumps(body()).replace('"confidence": 0.6', '"confidence": NaN')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw.encode())

    assert route(handler).route.error_type == "invalid_confidence"


def test_nan_probability_is_invalid() -> None:
    raw = json.dumps(body()).replace('"standard": 0.75', '"standard": NaN')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw.encode())

    assert route(handler).route.error_type == "invalid_probabilities"


@pytest.mark.parametrize(
    ("intent", "error_type"),
    [
        (None, "missing_answer"),
        ({**intent_answer(), "type": "score"}, "wrong_type"),
        # The 11 mock intents are not the 78 labels.
        (
            {
                "type": "choice",
                "choice": "card_arrival",
                "confidence": 0.9,
                "probabilities": {"card_arrival": 0.9, "unknown": 0.1},
            },
            "wrong_options",
        ),
        # A choice outside the options, with the probabilities over the labels.
        (intent_answer(choice="not_a_label"), "invalid_probabilities"),
        ({**intent_answer(), "choice": "unknown"}, "invalid_probabilities"),
    ],
)
def test_bad_intent_answer_keeps_the_route(intent: Any, error_type: str) -> None:
    payload = body()
    if intent is None:
        del payload["answers"]["intent"]
    else:
        payload["answers"]["intent"] = intent
    result = route(reply(payload))
    assert result.intent.error_type == error_type
    assert result.route.choice == "standard"


def test_every_label_can_be_the_intent() -> None:
    for label in INTENT_OPTIONS:
        result = route(reply(body(intent=intent_answer(choice=label))))
        assert result.intent.choice == label


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, "http_400"),
        (401, "http_401"),
        (413, "http_413"),
        (415, "http_415"),
        (422, "http_422"),
        (429, "http_429"),
        (529, "http_529"),
        (500, "http_5xx"),
        (503, "http_5xx"),
        (404, "http_other"),
        (201, "http_other"),
        (302, "http_other"),
    ],
)
def test_http_errors_fail_the_whole_call(status: int, error_type: str) -> None:
    error = call_error(reply({"error": "details"}, status=status))
    assert error.error_type == error_type
    assert not isinstance(error, TimeoutError)


def test_redirects_are_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.example/"})

    assert call_error(handler).error_type == "http_other"
    assert seen == ["https://api.typesafe.ai/v1/systemone"]


@pytest.mark.parametrize(
    ("content", "error_type"),
    [
        (b"<html>Bad gateway</html>", "bad_json"),
        (b"", "bad_json"),
        (b"\xff\xfe", "bad_json"),
        (b"[]", "bad_body"),
        (b"null", "bad_body"),
        (json.dumps({**body(), "answers": []}).encode(), "bad_body"),
        (
            json.dumps({k: v for k, v in body().items() if k != "usage"}).encode(),
            "bad_body",
        ),
        (
            json.dumps({k: v for k, v in body().items() if k != "model"}).encode(),
            "bad_body",
        ),
        (json.dumps({**body(), "model": ""}).encode(), "bad_body"),
        (
            json.dumps(
                {**body(), "usage": {"input_tokens": -1, "output_tokens": 0}}
            ).encode(),
            "bad_body",
        ),
        (
            json.dumps(
                {**body(), "usage": {"input_tokens": "9", "output_tokens": 0}}
            ).encode(),
            "bad_body",
        ),
        (
            json.dumps(
                {**body(), "usage": {"input_tokens": 9.5, "output_tokens": 0}}
            ).encode(),
            "bad_body",
        ),
    ],
)
def test_unreadable_bodies_fail_the_whole_call(content: bytes, error_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    assert call_error(handler).error_type == error_type


def test_client_timeout_counts_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    error = call_error(handler)
    assert isinstance(error, JevTimeout)
    assert isinstance(error, TimeoutError)
    assert error.error_type == "timeout"


def test_connection_failure_fails_the_whole_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert call_error(handler).error_type == "request_failed"


def test_errors_never_echo_the_key_message_or_provider_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": f"bad state: {MESSAGE} {KEY}"})

    error = call_error(handler)
    assert str(error) == "http_422"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_client_needs_a_key() -> None:
    with pytest.raises(ValueError, match="APP_JEV_API_KEY"):
        create_jev_http_client(Settings(_env_file=None))
    with pytest.raises(ValueError, match="APP_JEV_API_KEY"):
        create_jev_http_client(Settings(_env_file=None, jev_api_key=""))


def test_client_settings() -> None:
    client = create_jev_http_client(
        settings(jev_base_url="https://jev.example.test", jev_timeout_seconds=1.5)
    )
    assert str(client.base_url) == "https://jev.example.test/"
    assert client.timeout.read == 1.5
    assert client.follow_redirects is False
    assert KEY not in repr(client.headers)
    asyncio.run(client.aclose())


# Through the gateway: the real client in place of the mock router.


@pytest.fixture
def gateway_settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


def chat(gateway_settings: Settings, handler: Handler) -> dict[str, Any]:
    app = create_app(gateway_settings, make_router(handler))
    with TestClient(app) as client:
        response = client.post("/v1/chat", json={"message": MESSAGE})
        assert response.status_code == 200
        return response.json()


def test_gateway_uses_the_jev_route(gateway_settings: Settings) -> None:
    complex_route = {
        **ROUTE_ANSWER,
        "choice": "complex",
        "probabilities": {"standard": 0.25, "complex": 0.75},
    }
    trace = chat(gateway_settings, reply(body(route=complex_route)))["trace"]
    assert trace["jev_choice"] == "complex"
    assert trace["route"] == "complex"
    assert trace["fallback_reason"] is None


def test_gateway_falls_back_on_a_failed_call(
    gateway_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    trace = chat(gateway_settings, reply({"detail": KEY}, status=401))["trace"]
    assert trace["router"] == "rule_router"
    assert trace["fallback_reason"] == "jev_error"
    assert KEY not in caplog.text
    assert MESSAGE not in caplog.text


def test_gateway_counts_a_client_timeout_as_jev_timeout(
    gateway_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    assert chat(gateway_settings, handler)["trace"]["fallback_reason"] == "jev_timeout"


def test_gateway_falls_back_on_an_invalid_route(gateway_settings: Settings) -> None:
    payload = body(route={**ROUTE_ANSWER, "choice": "fast"})
    trace = chat(gateway_settings, reply(payload))["trace"]
    assert trace["router"] == "rule_router"
    assert trace["fallback_reason"] == "jev_invalid_route"


def test_gateway_keeps_the_route_when_the_intent_is_invalid(
    gateway_settings: Settings,
) -> None:
    payload = body(intent={**intent_answer(), "type": "score"})
    trace = chat(gateway_settings, reply(payload))["trace"]
    assert trace["jev_choice"] == "standard"
    assert trace["fallback_reason"] is None
