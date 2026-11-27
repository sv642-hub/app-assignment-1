"""Jev usage: answered calls, tokens and estimated cost, in /v1/health and
/metrics."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.router import JevResult, MockJevRouter
from app_assignment_1.service import Gateway

MESSAGE = "my card has not arrived"


class CountingJev(MockJevRouter):
    """The mock, but reporting tokens the way real Jev does."""

    async def route(self, message: str) -> JevResult:
        result = await super().route(message)
        return result.model_copy(update={"input_tokens": 2000, "output_tokens": 7})


class FailingJev(MockJevRouter):
    async def route(self, message: str) -> JevResult:
        raise ConnectionError("Jev is down")


def chat(g: Gateway, times: int) -> None:
    async def go() -> None:
        for _ in range(times):
            await g.chat(MESSAGE)

    asyncio.run(go())


def test_counts_answered_calls_tokens_and_cost():
    settings = Settings(_env_file=None, jev_input_price_per_million_usd=0.042)
    g = Gateway(settings, CountingJev(settings.intents_path))
    chat(g, 3)
    usage = g.jev_usage()
    # Each call counts once, not once per question.
    assert (usage.answered_calls, usage.input_tokens, usage.output_tokens) == (
        3,
        6000,
        21,
    )
    # Output tokens are free: 6000 input tokens at $0.042 per million.
    assert usage.estimated_cost_usd == pytest.approx(0.000252)


def test_cost_stays_null_without_a_price():
    settings = Settings(_env_file=None)
    g = Gateway(settings, CountingJev(settings.intents_path))
    chat(g, 2)
    assert g.jev_usage().estimated_cost_usd is None
    assert b"gateway_jev_estimated_cost_usd" not in g.metrics.render()


def test_failed_and_skipped_calls_are_not_counted():
    settings = Settings(_env_file=None, jev_breaker_failures=2)
    g = Gateway(settings, FailingJev(settings.intents_path))
    chat(g, 4)  # 2 failures open the breaker, then 2 calls are skipped
    usage = g.jev_usage()
    assert (usage.answered_calls, usage.input_tokens) == (0, 0)


def test_health_and_metrics_show_usage():
    settings = Settings(_env_file=None, jev_input_price_per_million_usd=0.042)
    app = create_app(settings, router=CountingJev(settings.intents_path))
    with TestClient(app) as client:
        client.post("/v1/chat", json={"message": MESSAGE})
        body = client.get("/v1/health").json()
        metrics = client.get("/metrics").text
    assert body["jev_usage"] == {
        "mocked": True,
        "answered_calls": 1,
        "input_tokens": 2000,
        "output_tokens": 7,
        "estimated_cost_usd": pytest.approx(0.000084),
    }
    assert 'gateway_jev_answered_calls_total{mocked="true"} 1.0' in metrics
    assert 'gateway_jev_tokens_total{direction="input",mocked="true"} 2000.0' in metrics
    assert 'gateway_jev_tokens_total{direction="output",mocked="true"} 7.0' in metrics
    assert 'gateway_jev_estimated_cost_usd_total{mocked="true"} 8.4e-05' in metrics


@pytest.mark.parametrize("price", ["1e400", "inf", "nan"])
def test_an_infinite_or_nan_price_is_rejected_at_startup(price):
    # 0 calls x inf is nan, which JevUsage rejects, so /v1/health would fail.
    with pytest.raises(ValueError, match="finite number"):
        Settings(_env_file=None, jev_input_price_per_million_usd=price)
