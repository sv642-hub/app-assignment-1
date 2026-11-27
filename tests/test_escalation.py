"""Escalation: the large model answers when the small model looks unsure."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.confidence import ScoredReply
from app_assignment_1.config import Settings
from app_assignment_1.schemas import BackendReply

SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"


class ImmediateRouter:
    async def route(self, message):
        # Force the rule router, which sends a short message to the small model.
        raise ConnectionError


def unknown(confidence: float | None) -> ScoredReply:
    return ScoredReply(
        intent="unknown", reply="", policy_facts=[], label_confidence=confidence
    )


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def metric(client: TestClient, line_start: str) -> float:
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(line_start + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{line_start} not found")


def outcome(client: TestClient, name: str) -> float:
    return metric(client, f'gateway_escalations_total{{outcome="{name}"}}')


@contextmanager
def run(
    config: Settings,
    small_score: float | None,
    large: object = "ok",
    message: str = "card",
) -> Iterator[tuple[TestClient, dict, list[str]]]:
    calls: list[str] = []

    async def small(message):
        calls.append(SMALL)
        return unknown(small_score)

    async def big(message):
        calls.append(LARGE)
        if large == "error":
            raise ConnectionError
        if large == "bad_format":
            return BackendReply(intent="not_a_label", reply="", policy_facts=[])
        return unknown(None)

    with TestClient(create_app(config, ImmediateRouter())) as client:
        gateway = client.app.state.gateway
        gateway.backends[0].generate = small
        gateway.backends[1].generate = big
        response = client.post("/v1/chat", json={"message": message})
        assert response.status_code == 200
        yield client, response.json(), calls


def test_off_by_default_reports_the_score_and_a_shadow_escalation() -> None:
    with run(settings(), 0.2) as (client, body, calls):
        assert calls == [SMALL]
        trace = body["trace"]
        assert trace["model"] == SMALL
        assert trace["escalation_score"] == 0.2
        assert trace["escalated"] is False
        assert outcome(client, "shadow") == 1
        assert outcome(client, "escalated") == 0
        assert metric(client, "gateway_escalation_score_count") == 1


def test_low_score_escalates_to_the_large_model() -> None:
    with run(settings(escalation_enabled=True), 0.2) as (client, body, calls):
        assert calls == [SMALL, LARGE]
        trace = body["trace"]
        assert trace["model"] == LARGE
        assert trace["route"] == "complex"
        assert trace["escalated"] is True
        assert trace["escalation_score"] == 0.2
        assert "low_confidence_escalation" in trace["fallback_reason"]
        assert outcome(client, "escalated") == 1
        # Both replies were monitored.
        samples = {
            row["name"]: row["samples"]
            for row in client.get("/v1/health").json()["monitor"]
        }
        assert samples == {SMALL: 1, LARGE: 1}
        # The stored request carries the same fields.
        stored = client.get(f"/v1/requests/{body['id']}").json()
        assert stored["trace"]["escalated"] is True


def test_score_at_or_above_the_threshold_keeps_the_small_reply() -> None:
    config = settings(escalation_enabled=True, escalation_threshold=0.5)
    with run(config, 0.5) as (client, body, calls):
        assert calls == [SMALL]
        assert body["trace"]["escalated"] is False
        assert all(
            outcome(client, name) == 0
            for name in ("escalated", "shadow", "large_failed")
        )


def test_no_score_means_no_escalation() -> None:
    with run(settings(escalation_enabled=True), None) as (client, body, calls):
        assert calls == [SMALL]
        assert body["trace"]["escalation_score"] is None
        assert metric(client, "gateway_escalation_score_count") == 0


@pytest.mark.parametrize(
    ("large", "expected"),
    [("error", "large_failed"), ("bad_format", "large_format_failed")],
)
def test_a_failed_large_reply_keeps_the_small_reply(large, expected) -> None:
    with run(settings(escalation_enabled=True), 0.2, large) as (client, body, calls):
        assert calls == [SMALL, LARGE]
        trace = body["trace"]
        assert trace["model"] == SMALL
        assert trace["format_check"] == "passed"
        assert trace["escalated"] is False
        assert trace["escalation_score"] == 0.2
        assert outcome(client, expected) == 1


def test_a_disabled_large_model_is_not_called() -> None:
    config = settings(escalation_enabled=True, operator_token="t" * 16)
    client = TestClient(create_app(config, ImmediateRouter()))
    with client:
        calls: list[str] = []

        async def small(message):
            calls.append(SMALL)
            return unknown(0.2)

        async def big(message):
            calls.append(LARGE)
            return unknown(None)

        gateway = client.app.state.gateway
        gateway.backends[0].generate = small
        gateway.backends[1].generate = big
        disabled = client.post(
            f"/v1/operator/models/{LARGE}/disable",
            headers={"Authorization": "Bearer " + "t" * 16},
            json={"reason": "test"},
        )
        assert disabled.status_code == 200, disabled.text
        body = client.post("/v1/chat", json={"message": "card"}).json()
        assert calls == [SMALL]
        assert body["trace"]["model"] == SMALL
        assert outcome(client, "large_unusable") == 1


def test_only_the_small_model_is_scored() -> None:
    # One word is enough for "complex", so the large model answers first.
    config = settings(escalation_enabled=True, rule_router_complex_min_words=1)
    client = TestClient(create_app(config, ImmediateRouter()))
    with client:

        async def big(message):
            return unknown(0.1)

        client.app.state.gateway.backends[1].generate = big
        body = client.post("/v1/chat", json={"message": "card"}).json()
        assert body["trace"]["model"] == LARGE
        assert body["trace"]["escalation_score"] is None
        assert metric(client, "gateway_escalation_score_count") == 0


def test_mock_models_escalate_a_message_they_cannot_match() -> None:
    config = settings(escalation_enabled=True)
    with TestClient(create_app(config, ImmediateRouter())) as client:
        body = client.post("/v1/chat", json={"message": "zzz qqq"}).json()
        assert body["trace"]["escalation_score"] == 0.3
        assert body["trace"]["escalated"] is True
        assert body["trace"]["model"] == LARGE


def test_a_large_call_cut_short_by_the_deadline_is_no_time() -> None:
    config = settings(
        escalation_enabled=True,
        request_deadline_seconds=3,
        jev_timeout_seconds=1,
        alternate_reserve_seconds=0.5,
    )
    with TestClient(create_app(config, ImmediateRouter())) as client:

        async def small(message):
            return unknown(0.2)

        async def big(message):
            await asyncio.sleep(10)
            return unknown(None)

        client.app.state.gateway.backends[0].generate = small
        client.app.state.gateway.backends[1].generate = big
        body = client.post("/v1/chat", json={"message": "card"}).json()
        assert body["trace"]["model"] == SMALL
        assert outcome(client, "no_time") == 1
        assert outcome(client, "large_failed") == 0
