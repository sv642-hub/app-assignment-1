"""Gateway budgets must preserve fallback and avoid scoring truncated calls."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.schemas import BackendReply


class ImmediateRouter:
    async def route(self, message):
        # Deliberately force the supported rule-router fallback.
        raise ConnectionError


@pytest.mark.parametrize("alternate", ["success", "hang", "own_timeout"])
def test_first_timeout_preserves_alternate_and_scores_correctly(alternate):
    settings = Settings(
        _env_file=None,
        jev_timeout_seconds=0.01,
        request_deadline_seconds=0.2,
        alternate_reserve_seconds=0.1,
    )
    cancelled = []

    async def hung(message):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def second(message):
        if alternate == "hang":
            return await hung(message)
        if alternate == "own_timeout":
            raise TimeoutError
        return BackendReply(
            intent="unknown",
            reply="Please describe your banking problem.",
            policy_facts=[],
        )

    with TestClient(create_app(settings, ImmediateRouter())) as client:
        gateway = client.app.state.gateway
        gateway.backends[0].generate = hung
        gateway.backends[1].generate = second
        response = client.post("/v1/chat", json={"message": "card"})
        assert response.status_code == (200 if alternate == "success" else 503)
        health = client.get("/v1/health").json()
        small, large = health["usage"]
        assert small["failed_calls"] == 1
        assert large["attempted_calls"] == 1
        assert large["deadline_exceeded_calls"] == (alternate == "hang")
        assert large["failed_calls"] == (alternate == "own_timeout")
        samples = {row["name"]: row["samples"] for row in health["monitor"]}
        assert samples[small["name"]] == 1
        assert samples[large["name"]] == (alternate != "hang")
        assert len(cancelled) == (2 if alternate == "hang" else 1)
        if alternate == "hang":
            assert response.json()["detail"] == "deadline_exceeded"


@pytest.mark.parametrize("lone", ["small", "large"])
def test_a_lone_model_timeout_gives_the_usual_503(lone):
    # The other model is failure-disabled, so the selected model gets all the
    # time and its timeout is a model failure. The other model's cooldown ends
    # during that call, but no time is left for it. Nothing was cut short by
    # the deadline, so this is the usual 503, not deadline_exceeded. With the
    # large model selected, the small one comes back in the second pass.
    settings = Settings(
        _env_file=None,
        jev_timeout_seconds=0.01,
        request_deadline_seconds=0.3,
        alternate_reserve_seconds=0.1,
        # One word is enough for "complex", so "card" goes to the large model.
        rule_router_complex_min_words=1 if lone == "large" else 20,
    )

    async def hung(message):
        await asyncio.Event().wait()

    with TestClient(create_app(settings, ImmediateRouter())) as client:
        gateway = client.app.state.gateway
        small, large = gateway.backends
        on, off = (small, large) if lone == "small" else (large, small)
        gateway.controller.disabled_until[off.config.name] = (
            gateway.controller.clock() + 0.1
        )
        on.generate = hung
        response = client.post("/v1/chat", json={"message": "card"})
        assert response.status_code == 503
        assert response.json()["detail"] == "No mock backend is available"
        usage = {row["name"]: row for row in client.get("/v1/health").json()["usage"]}
        assert usage[on.config.name]["failed_calls"] == 1
        assert usage[on.config.name]["deadline_exceeded_calls"] == 0
        assert usage[off.config.name]["attempted_calls"] == 0


@pytest.mark.parametrize("disabled", ["selected", "alternate"])
def test_only_usable_model_gets_the_full_remaining_time(disabled):
    # With the reserve held back, the only usable model would get 0.4 s and
    # time out on a 0.6 s reply. With nothing to fall back to, it gets ~1 s.
    settings = Settings(
        _env_file=None,
        jev_timeout_seconds=0.01,
        request_deadline_seconds=1.0,
        alternate_reserve_seconds=0.6,
    )

    async def slow(message):
        await asyncio.sleep(0.6)
        return BackendReply(
            intent="unknown",
            reply="Please describe your banking problem.",
            policy_facts=[],
        )

    with TestClient(create_app(settings, ImmediateRouter())) as client:
        gateway = client.app.state.gateway
        small, large = gateway.backends
        off, on = (small, large) if disabled == "selected" else (large, small)
        gateway.controller.disabled_until[off.config.name] = (
            gateway.controller.clock() + 3600
        )
        on.generate = slow
        response = client.post("/v1/chat", json={"message": "card"})
        assert response.status_code == 200
        assert response.json()["trace"]["model"] == on.config.name
        usage = {row["name"]: row for row in client.get("/v1/health").json()["usage"]}
        assert usage[on.config.name]["completed_calls"] == 1
        assert usage[on.config.name]["failed_calls"] == 0
        assert usage[on.config.name]["deadline_exceeded_calls"] == 0
        assert usage[off.config.name]["attempted_calls"] == 0
