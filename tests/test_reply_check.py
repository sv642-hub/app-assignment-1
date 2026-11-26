"""The second Jev call: it runs after the reply, never delays the customer, is
off by default, and compares with the first call."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.jev import REPLY_MARKER, TypeSafeJevRouter, create_jev_http_client
from app_assignment_1.labels import load_labels
from app_assignment_1.records import ReplyCheck
from app_assignment_1.router import (
    JevAnswer,
    MockJevRouter,
    ReplyCheckResult,
)
from app_assignment_1.service import Gateway
from app_assignment_1.store import MemoryStore

KEY = "tsk-Hunter2"
LABELS = load_labels(Path("config/labels.json"))
INTENT_OPTIONS = [item.label for item in LABELS.items]
ROUTE_ANSWER = {
    "type": "choice",
    "choice": "standard",
    "confidence": 0.6,
    "probabilities": {"standard": 0.75, "complex": 0.25},
}


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


MESSAGE = "my card has not arrived"


@pytest.fixture
def backends_path(tmp_path: Path) -> Path:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return path


def make_settings(backends_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        backends_path=backends_path,
        jev_input_price_per_million_usd=1,
        **overrides,
    )


def metric(client: TestClient, name: str, **labels: str) -> float:
    total = 0.0
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name == name and labels.items() <= sample.labels.items():
                total += sample.value
    return total


def chat(client: TestClient, message: str = MESSAGE) -> dict[str, Any]:
    response = client.post("/v1/chat", json={"message": message})
    assert response.status_code == 200
    return response.json()


def settle(client: TestClient) -> None:
    gateway: Gateway = client.app.state.gateway  # type: ignore[attr-defined]
    client.portal.call(gateway.finish_saves)  # type: ignore[union-attr]


def test_off_by_default_makes_no_second_call(backends_path: Path) -> None:
    store = MemoryStore(100)
    calls: list[str] = []

    class Spy(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            calls.append(message)
            return await super().check_reply(message, reply)

    router = Spy(Path("config/intents.json"))
    with TestClient(
        create_app(make_settings(backends_path), router=router, store=store)
    ) as client:
        chat(client)
        settle(client)
        assert metric(client, "gateway_reply_check_enabled") == 0
        assert metric(client, "gateway_reply_check_latency_seconds_count") == 0
    assert calls == []
    assert not store.reply_checks


def test_enabled_records_agreement_and_metrics(backends_path: Path) -> None:
    store = MemoryStore(100)
    settings = make_settings(backends_path, jev_reply_check_enabled=True)
    with TestClient(create_app(settings, store=store)) as client:
        body = chat(client)
        settle(client)
        assert metric(client, "gateway_reply_check_enabled") == 1
        assert metric(client, "gateway_reply_check_answered_calls_total") == 1
        assert (
            metric(client, "gateway_reply_check_latency_seconds_count", outcome="ok")
            == 1
        )
        assert (
            metric(client, "gateway_reply_check_agreement_total", question="intent")
            == 1
        )
    [check] = store.reply_checks
    assert str(check.request_id) == body["id"]
    assert check.status == "ok"
    assert check.mocked is True
    assert check.intent_first == "card_arrival"
    assert check.intent_second == check.intent_first
    assert check.intent_agrees is True
    assert check.route_agrees is True
    assert check.reply_fit in ("fits", "does_not_fit")
    # Neither the message nor the reply is in the saved record.
    dumped = check.model_dump_json()
    assert MESSAGE not in dumped and body["reply"] not in dumped


def test_customer_never_waits_for_the_second_call(backends_path: Path) -> None:
    class Slow(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            await asyncio.sleep(0.3)
            return await super().check_reply(message, reply)

    store = MemoryStore(100)
    settings = make_settings(backends_path, jev_reply_check_enabled=True)
    router = Slow(Path("config/intents.json"))
    with TestClient(create_app(settings, router=router, store=store)) as client:
        chat(client)
        # The reply and the request save are done; the check is still running.
        assert not store.reply_checks
        assert len(store.requests) == 1
        settle(client)
        assert len(store.reply_checks) == 1


def test_failed_second_call_is_recorded_and_harmless(
    backends_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            raise ConnectionError(f"secret {message}")

    store = MemoryStore(100)
    settings = make_settings(backends_path, jev_reply_check_enabled=True)
    router = Broken(Path("config/intents.json"))
    caplog.set_level(logging.WARNING)
    with TestClient(create_app(settings, router=router, store=store)) as client:
        for _ in range(8):
            chat(client)
        settle(client)
        assert (
            metric(client, "gateway_reply_check_latency_seconds_count", outcome="error")
            == 8
        )
        # The second call's failures never open the first call's breaker.
        assert client.get("/v1/health").json()["jev_breaker"]["state"] == "closed"
    assert {c.status for c in store.reply_checks} == {"error"}
    assert all(c.intent_agrees is None for c in store.reply_checks)
    assert "secret" not in caplog.text and MESSAGE not in caplog.text


def test_timeout_is_recorded(backends_path: Path) -> None:
    class Hangs(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            await asyncio.sleep(5)
            raise AssertionError

    store = MemoryStore(100)
    settings = make_settings(
        backends_path,
        jev_reply_check_enabled=True,
        jev_reply_check_timeout_seconds=0.05,
    )
    router = Hangs(Path("config/intents.json"))
    with TestClient(create_app(settings, router=router, store=store)) as client:
        chat(client)
        settle(client)
    [check] = store.reply_checks
    assert (check.status, check.error_type) == ("timeout", "timeout")


def test_disagreement_is_counted(backends_path: Path) -> None:
    class Disagrees(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            real = await super().check_reply(message, reply)
            other = "lost_or_stolen_card"
            return real.model_copy(
                update={
                    "intent": JevAnswer(
                        choice=other,
                        confidence=0.6,
                        probabilities=self._spread(other, 0.6),
                    ),
                    "fit": JevAnswer(
                        choice="does_not_fit",
                        confidence=0.8,
                        probabilities={"does_not_fit": 0.8, "fits": 0.2},
                    ),
                }
            )

    store = MemoryStore(100)
    settings = make_settings(backends_path, jev_reply_check_enabled=True)
    router = Disagrees(Path("config/intents.json"))
    with TestClient(create_app(settings, router=router, store=store)) as client:
        chat(client)
        settle(client)
        assert (
            metric(
                client,
                "gateway_reply_check_agreement_total",
                question="intent",
                result="disagree",
            )
            == 1
        )
        assert (
            metric(client, "gateway_reply_check_fit_total", result="does_not_fit") == 1
        )
    [check] = store.reply_checks
    assert check.intent_agrees is False and check.reply_fit == "does_not_fit"


def test_skipped_without_a_first_answer(
    backends_path: Path,
) -> None:
    class NoFirst(MockJevRouter):
        async def route(self, message: str) -> Any:
            raise ConnectionError("down")

    store = MemoryStore(100)
    settings = make_settings(backends_path, jev_reply_check_enabled=True)
    router = NoFirst(Path("config/intents.json"))
    with TestClient(create_app(settings, router=router, store=store)) as client:
        chat(client)
        settle(client)
        assert (
            metric(
                client, "gateway_reply_check_skipped_total", reason="no_first_answer"
            )
            == 1
        )
    assert not store.reply_checks


def test_too_many_checks_in_flight_are_skipped(backends_path: Path) -> None:
    class Slow(MockJevRouter):
        async def check_reply(self, message: str, reply: str) -> ReplyCheckResult:
            await asyncio.sleep(0.3)
            return await super().check_reply(message, reply)

    store = MemoryStore(100)
    settings = make_settings(
        backends_path, jev_reply_check_enabled=True, jev_reply_check_max_in_flight=1
    )
    router = Slow(Path("config/intents.json"))
    with TestClient(create_app(settings, router=router, store=store)) as client:
        chat(client)
        chat(client)
        settle(client)
        assert metric(client, "gateway_reply_check_skipped_total", reason="busy") == 1
    assert len(store.reply_checks) == 1


def test_enabled_needs_a_router_that_can_check(backends_path: Path) -> None:
    class Plain:
        mocked = True
        option_set_version = "x"

        async def route(self, message: str) -> Any:
            raise NotImplementedError

    with pytest.raises(ValueError, match="REPLY_CHECK"):
        Gateway(make_settings(backends_path, jev_reply_check_enabled=True), Plain())  # type: ignore[arg-type]


def test_mock_check_follows_the_message_and_the_reply() -> None:
    router = MockJevRouter(Path("config/intents.json"))
    fits = asyncio.run(router.check_reply(MESSAGE, "Your card should arrive soon."))
    assert fits.intent.choice == "card_arrival"
    assert fits.mocked and fits.input_tokens == 0
    other = asyncio.run(
        router.check_reply(MESSAGE, "Report a stolen card to us at once.")
    )
    assert other.fit.choice == "does_not_fit"


def test_reply_check_record_rules() -> None:
    fields: dict[str, Any] = dict(
        request_id="6f1f1f1e-1111-4111-8111-111111111111",
        call_id="6f1f1f1e-2222-4222-8222-222222222222",
        finished_at="2026-11-26T08:00:00Z",
        mocked=True,
        option_set_version="v",
        latency_ms=5,
    )
    ok = dict(
        fields,
        status="ok",
        jev_model="mock-jev",
        input_tokens=0,
        output_tokens=0,
        intent_first="a",
        intent_second="a",
        intent_agrees=True,
    )
    ReplyCheck(**ok)
    with pytest.raises(ValidationError):  # agrees must match the answers
        ReplyCheck(**dict(ok, intent_agrees=False))
    with pytest.raises(ValidationError):  # no verdict without both answers
        ReplyCheck(**dict(ok, intent_second=None))
    with pytest.raises(ValidationError):  # a failed call needs an error type
        ReplyCheck(**fields, status="error")
    with pytest.raises(ValidationError):  # and has no second answers
        ReplyCheck(**fields, status="error", error_type="x", intent_second="a")


def test_real_client_sends_message_and_reply_with_three_questions() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        fit = {
            "type": "choice",
            "choice": "fits",
            "confidence": 0.9,
            "probabilities": {"fits": 0.9, "does_not_fit": 0.1},
        }
        return httpx.Response(
            200,
            json={
                "model": "jev-1.14.0",
                "answers": {
                    "route": ROUTE_ANSWER,
                    "intent": intent_answer(),
                    "fit": fit,
                },
                "usage": {"input_tokens": 1700, "output_tokens": 30},
            },
        )

    client = create_jev_http_client(
        Settings(_env_file=None, jev_api_key=KEY), httpx.MockTransport(handler)
    )
    router = TypeSafeJevRouter(client, "jev-1.14.0", LABELS)
    result = asyncio.run(router.check_reply(MESSAGE, "It is on the way."))
    [sent] = seen
    assert sent["state"] == f"{MESSAGE}{REPLY_MARKER}It is on the way."
    assert set(sent["questions"]) == {"route", "intent", "fit"}
    assert set(sent["questions"]["fit"]["criteria"]) == {"fits", "does_not_fit"}
    assert list(sent["questions"]["intent"]["criteria"]) == INTENT_OPTIONS
    assert result.fit.choice == "fits" and result.intent.choice == "card_arrival"
    assert result.input_tokens == 1700
    # The first call still asks two questions.
    asyncio.run(router.route(MESSAGE))
    assert set(seen[-1]["questions"]) == {"route", "intent"}
    assert load_labels(Path("config/labels.json")).option_set_version
