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
