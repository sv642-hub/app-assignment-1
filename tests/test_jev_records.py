"""The gateway writes both Jev assessments per request, and the circuit breaker."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.breaker import CircuitBreaker
from app_assignment_1.config import Settings
from app_assignment_1.jev import TypeSafeJevRouter, create_jev_http_client
from app_assignment_1.labels import load_labels
from app_assignment_1.records import InMemoryRecordSink, JevAssessment, check_call
from app_assignment_1.router import MockJevRouter
from app_assignment_1.service import Gateway

MESSAGE = "my card has not arrived"
LABELS = load_labels(Path("config/labels.json"))
OPTIONS = [item.label for item in LABELS.items]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
        backend["error_rate"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


def jev_body(route: dict[str, Any] | None = None) -> dict[str, Any]:
    intent_probabilities = {label: 0.1 / 77 for label in OPTIONS}
    intent_probabilities["card_arrival"] = 0.9
    return {
        "model": "jev-1.14.0",
        "answers": {
            "route": route
            or {
                "type": "choice",
                "choice": "complex",
                "confidence": 0.5,
                "probabilities": {"standard": 0.3, "complex": 0.7},
            },
            "intent": {
                "type": "choice",
                "choice": "card_arrival",
                "confidence": 0.8,
                "probabilities": intent_probabilities,
            },
        },
        "usage": {"input_tokens": 1500, "output_tokens": 40},
    }


class FakeJev:
    """A fake TypeSafe server that answers or fails, and counts calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.status = 200
        self.payload: dict[str, Any] = jev_body()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            self.status,
            json=self.payload,
            headers={"x-typesafe-request-id": "req_1"},
        )


def real_router(server: FakeJev) -> TypeSafeJevRouter:
    client = create_jev_http_client(
        Settings(_env_file=None, jev_api_key="tsk-test"), httpx.MockTransport(server)
    )
    return TypeSafeJevRouter(client, "jev-1.14.0", LABELS)


def gateway(settings: Settings, router: Any, clock: Clock | None = None):
    sink = InMemoryRecordSink()
    return Gateway(settings, router, clock=clock or Clock(), jev_records=sink), sink


def chat(gw: Gateway) -> Any:
    return asyncio.run(gw.chat(MESSAGE))


def pair(sink: InMemoryRecordSink) -> dict[str, JevAssessment]:
    """The last call's two records, checked as one call."""
    records = sink.jev[-2:]
    check_call(records)
    return {record.question: record for record in records}


# Records


def test_mock_request_writes_two_ok_records(settings: Settings) -> None:
    gw, sink = gateway(settings, MockJevRouter(settings.intents_path))
    response = chat(gw)
    assert len(sink.jev) == 2
    records = pair(sink)
    for record in records.values():
        assert record.request_id == response.id
        assert record.status == "ok"
        assert record.mocked is True
        assert record.option_set_version.startswith("mock-intents-")
        assert record.jev_model == "mock-jev"
        assert record.latency_ms is not None and record.latency_ms >= 0
    assert records["route"].choice == response.trace.route
    assert records["intent"].choice == "card_arrival"


def test_each_call_gets_its_own_call_id(settings: Settings) -> None:
    gw, sink = gateway(settings, MockJevRouter(settings.intents_path))
    first, second = chat(gw), chat(gw)
    assert first.id != second.id
    assert len({record.call_id for record in sink.jev}) == 2


def test_real_call_records_the_call_details(settings: Settings) -> None:
    gw, sink = gateway(settings, real_router(FakeJev()))
    chat(gw)
    records = pair(sink)
    for record in records.values():
        assert record.status == "ok"
        assert record.mocked is False
        assert record.option_set_version == LABELS.option_set_version
        assert record.jev_model == "jev-1.14.0"
        assert record.provider_request_id == "req_1"
        assert (record.input_tokens, record.output_tokens) == (1500, 40)
    assert set(records["intent"].probabilities or {}) == set(OPTIONS)


def test_records_never_hold_the_message(settings: Settings) -> None:
    gw, sink = gateway(settings, real_router(FakeJev()))
    chat(gw)
    for record in sink.jev:
        assert MESSAGE not in record.model_dump_json()


def test_invalid_route_answer_records_an_error_and_keeps_the_intent(
    settings: Settings,
) -> None:
    server = FakeJev()
    server.payload = jev_body(route={"type": "noul", "noul": 0.9})
    gw, sink = gateway(settings, real_router(server))
    response = chat(gw)
    assert response.trace.fallback_reason == "jev_invalid_route"
    records = pair(sink)
    assert records["route"].status == "error"
    assert records["route"].error_type == "wrong_type"
    # The call still happened, so both records carry its details.
    assert records["route"].input_tokens == 1500
    assert records["intent"].status == "ok"
    assert records["intent"].choice == "card_arrival"


@pytest.mark.parametrize(
    ("status", "error_type"), [(401, "http_401"), (429, "http_429"), (529, "http_529")]
)
def test_http_failure_records_both_questions_as_errors(
    settings: Settings, status: int, error_type: str
) -> None:
    server = FakeJev()
    server.status = status
    gw, sink = gateway(settings, real_router(server))
    assert chat(gw).trace.fallback_reason == "jev_error"
    for record in pair(sink).values():
        assert record.status == "error"
        assert record.error_type == error_type
        assert record.jev_model is None
        assert record.input_tokens is None
        assert record.latency_ms is not None


def test_malformed_body_records_bad_json(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = create_jev_http_client(
        Settings(_env_file=None, jev_api_key="tsk-test"), httpx.MockTransport(handler)
    )
    gw, sink = gateway(settings, TypeSafeJevRouter(client, "jev-1.14.0", LABELS))
    chat(gw)
    assert {r.error_type for r in sink.jev} == {"bad_json"}


class SlowRouter:
    mocked = False
    option_set_version = "labels-test"

    def __init__(self) -> None:
        self.calls = 0

    async def route(self, message: str) -> Any:
        self.calls += 1
        await asyncio.sleep(10)


def test_timeout_records_both_questions_as_timeouts(settings: Settings) -> None:
    fast = settings.model_copy(update={"jev_timeout_seconds": 0.01})
    gw, sink = gateway(fast, SlowRouter())
    assert chat(gw).trace.fallback_reason == "jev_timeout"
    for record in pair(sink).values():
        assert record.status == "timeout"
        assert record.error_type == "timeout"
        assert record.option_set_version == "labels-test"
        assert record.latency_ms is not None and record.latency_ms >= 10


def test_cancelled_request_records_cancelled(settings: Settings) -> None:
    gw, sink = gateway(settings, SlowRouter())

    async def run() -> None:
        task = asyncio.create_task(gw.chat(MESSAGE))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    for record in pair(sink).values():
        assert record.status == "timeout"
        assert record.error_type == "cancelled"
    # A cancelled call does not count against Jev.
    assert gw.jev_breaker.consecutive_failures == 0


def test_a_cancelled_trial_lets_the_next_message_try(settings: Settings) -> None:
    clock = Clock()
    gw, _ = gateway(settings, SlowRouter(), clock)
    for _ in range(5):
        gw.jev_breaker.failed()
    clock.now += 30

    async def run() -> None:
        task = asyncio.create_task(gw.chat(MESSAGE))
        await asyncio.sleep(0.01)
        assert not gw.jev_breaker.allow()  # the trial call is running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert gw.jev_breaker.state() == "half_open"
    assert gw.jev_breaker.allow()


class BrokenRouter:
    mocked = False
    option_set_version = "labels-test"

    def __init__(self) -> None:
        self.calls = 0

    async def route(self, message: str) -> Any:
        self.calls += 1
        raise RuntimeError("provider said: tsk-secret " + message)


def test_unexpected_router_errors_get_a_generic_code(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    gw, sink = gateway(settings, BrokenRouter())
    chat(gw)
    assert {r.error_type for r in sink.jev} == {"router_error"}
    assert "tsk-secret" not in caplog.text
    assert MESSAGE not in caplog.text


def test_invalid_router_output_gets_invalid_result(settings: Settings) -> None:
    class BadRouter:
        mocked = False
        option_set_version = "labels-test"

        async def route(self, message: str) -> Any:
            return {"route": "complex"}

    gw, sink = gateway(settings, BadRouter())
    assert chat(gw).trace.fallback_reason == "jev_error"
    assert {r.error_type for r in sink.jev} == {"invalid_result"}


def test_a_failing_sink_does_not_fail_the_request(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingSink:
        def write_jev(self, record: JevAssessment) -> None:
            raise OSError("disk full")

    gw = Gateway(
        settings, MockJevRouter(settings.intents_path), jev_records=FailingSink()
    )
    assert chat(gw).trace.router == "mock_jev"
    assert "Could not write Jev records (OSError)" in caplog.text


def test_without_a_sink_records_are_dropped(settings: Settings) -> None:
    gw = Gateway(settings, MockJevRouter(settings.intents_path))
    assert chat(gw).trace.router == "mock_jev"


# Circuit breaker


def test_breaker_opens_after_the_failure_limit() -> None:
    clock = Clock()
    breaker = CircuitBreaker(3, 30, clock)
    for _ in range(2):
        assert breaker.allow()
        breaker.failed()
    assert breaker.state() == "closed"
    assert breaker.allow()
    breaker.failed()
    assert breaker.state() == "open"
    assert not breaker.allow()


def test_a_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(3, 30, Clock())
    breaker.failed()
    breaker.failed()
    breaker.succeeded()
    breaker.failed()
    breaker.failed()
    assert breaker.state() == "closed"


def test_after_the_cooldown_one_trial_call_runs() -> None:
    clock = Clock()
    breaker = CircuitBreaker(1, 30, clock)
    breaker.failed()
    clock.now += 29.9
    assert not breaker.allow()
    clock.now += 0.1
    assert breaker.state() == "half_open"
    assert breaker.allow()
    # Others wait while the trial call runs.
    assert not breaker.allow()
    breaker.succeeded()
    assert breaker.state() == "closed"
    assert breaker.allow()


def test_a_failed_trial_reopens_for_another_cooldown() -> None:
    clock = Clock()
    breaker = CircuitBreaker(5, 30, clock)
    for _ in range(5):
        breaker.failed()
    clock.now += 30
    assert breaker.allow()
    breaker.failed()
    assert breaker.state() == "open"
    clock.now += 29
    assert not breaker.allow()
    clock.now += 1
    assert breaker.allow()


def test_an_abandoned_trial_lets_the_next_message_try() -> None:
    clock = Clock()
    breaker = CircuitBreaker(1, 30, clock)
    breaker.failed()
    clock.now += 30
    assert breaker.allow()
    breaker.abandoned()
    assert breaker.state() == "half_open"
    assert breaker.allow()


def test_gateway_skips_jev_while_the_breaker_is_open(settings: Settings) -> None:
    clock = Clock()
    router = BrokenRouter()
    gw, sink = gateway(settings, router, clock)
    for _ in range(5):
        assert chat(gw).trace.fallback_reason == "jev_error"
    assert router.calls == 5

    response = chat(gw)
    assert router.calls == 5
    assert response.trace.fallback_reason == "jev_circuit_open"
    assert response.trace.router == "rule_router"
    for record in pair(sink).values():
        assert record.status == "skipped"
        assert record.skip_reason == "circuit_open"
        assert record.error_type is None
        assert record.latency_ms is None

    clock.now += 30
    chat(gw)
    assert router.calls == 6
    assert gw.jev_breaker.state() == "open"


def test_gateway_closes_the_breaker_when_jev_recovers(settings: Settings) -> None:
    clock = Clock()
    server = FakeJev()
    server.status = 529
    gw, sink = gateway(settings, real_router(server), clock)
    for _ in range(5):
        chat(gw)
    assert gw.jev_breaker.state() == "open"
    server.status = 200
    clock.now += 30
    response = chat(gw)
    assert response.trace.fallback_reason is None
    assert response.trace.jev_choice == "complex"
    assert gw.jev_breaker.state() == "closed"
    assert server.calls == 6


def test_an_invalid_answer_does_not_count_as_a_failure(settings: Settings) -> None:
    server = FakeJev()
    server.payload = jev_body(route={"type": "noul", "noul": 0.9})
    gw, _ = gateway(settings, real_router(server))
    for _ in range(6):
        assert chat(gw).trace.fallback_reason == "jev_invalid_route"
    assert server.calls == 6
    assert gw.jev_breaker.state() == "closed"


def test_breaker_limits_come_from_settings(settings: Settings) -> None:
    custom = settings.model_copy(
        update={"jev_breaker_failures": 2, "jev_breaker_cooldown_seconds": 5}
    )
    router = BrokenRouter()
    gw, _ = gateway(custom, router)
    for _ in range(3):
        chat(gw)
    assert router.calls == 2
    assert gw.jev_breaker.cooldown == 5


def test_health_shows_the_breaker(settings: Settings) -> None:
    clock = Clock()
    router = BrokenRouter()
    with TestClient(create_app(settings, router)) as client:
        client.app.state.gateway.jev_breaker.clock = clock
        health = client.get("/v1/health").json()
        assert health["status"] == "ok"
        assert health["jev_breaker"] == {
            "state": "closed",
            "consecutive_failures": 0,
            "failure_limit": 5,
            "cooldown_seconds": 30,
        }
        for _ in range(5):
            client.post("/v1/chat", json={"message": MESSAGE})
        health = client.get("/v1/health").json()
        assert health["status"] == "degraded"
        assert health["jev_breaker"]["state"] == "open"
        assert health["jev_breaker"]["consecutive_failures"] == 5
        clock.now += 30
        assert client.get("/v1/health").json()["jev_breaker"]["state"] == "half_open"


def test_metrics_count_circuit_open_fallbacks(settings: Settings) -> None:
    with TestClient(create_app(settings, BrokenRouter())) as client:
        for _ in range(7):
            client.post("/v1/chat", json={"message": MESSAGE})
        text = client.get("/metrics").text
    assert 'gateway_jev_fallbacks_total{reason="jev_error"} 5.0' in text
    assert 'gateway_jev_fallbacks_total{reason="jev_circuit_open"} 2.0' in text


def test_a_record_problem_does_not_fail_the_request(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    def broken_check(records: Any) -> None:
        raise ValueError("answer was card_arrival")

    monkeypatch.setattr("app_assignment_1.service.check_call", broken_check)
    gw, sink = gateway(settings, MockJevRouter(settings.intents_path))
    assert chat(gw).trace.router == "mock_jev"
    assert sink.jev == []
    assert "Could not build Jev records (ValueError)" in caplog.text
    assert "card_arrival" not in caplog.text
