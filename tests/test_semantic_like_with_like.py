"""The semantic monitor only compares like with like: a mock model with mock
Jev, or a real model with real Jev. Real Jev answers over 78 labels while the
mock models know 10 intents, so a mix would disagree by design."""

import asyncio

from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.router import JevResult, MockJevRouter
from app_assignment_1.service import Gateway

STANDARD = "my card has not arrived"
SMALL = "qwen3-1.7b"


class RealLookingJev(MockJevRouter):
    """Answers like the mock, but reports itself as real Jev."""

    mocked = False

    async def route(self, message: str) -> JevResult:
        result = await super().route(message)
        return result.model_copy(update={"mocked": False})


def chat(g: Gateway, times: int) -> None:
    async def go() -> None:
        for _ in range(times):
            await g.chat(STANDARD)

    asyncio.run(go())


def status(g: Gateway, name: str = SMALL):
    return next(m for m in g.semantic.stats() if m.name == name)


def test_mock_models_are_compared_with_mock_jev():
    settings = Settings(_env_file=None)
    g = Gateway(settings, MockJevRouter(settings.intents_path))
    chat(g, 3)
    small = status(g)
    assert (small.samples, small.agreements, small.not_compared) == (3, 3, 0)


def test_real_jev_is_not_compared_with_a_mock_model():
    settings = Settings(_env_file=None)
    g = Gateway(settings, RealLookingJev(settings.intents_path))
    assert g.jev_mocked is False
    chat(g, 4)
    small = status(g)
    assert (small.samples, small.status, small.not_compared) == (0, "collecting", 4)
    metrics = g.metrics.render().decode()
    assert 'gateway_semantic_not_compared_total{model="qwen3-1.7b"} 4.0' in metrics
    assert (
        'gateway_semantic_samples_total{model="qwen3-1.7b",sample="agree",'
        'traffic="own"} 0.0'
    ) in metrics


def test_a_real_model_is_not_compared_with_mock_jev():
    settings = Settings(_env_file=None)
    g = Gateway(settings, MockJevRouter(settings.intents_path))
    g.backends[0].mocked = False  # stands in for VLLMBackend
    chat(g, 2)
    assert (status(g).samples, status(g).not_compared) == (0, 2)


def test_health_reports_replies_not_compared():
    settings = Settings(_env_file=None)
    app = create_app(settings, router=RealLookingJev(settings.intents_path))
    with TestClient(app) as client:
        client.post("/v1/chat", json={"message": STANDARD})
        body = client.get("/v1/health").json()
    small = next(m for m in body["semantic"] if m["name"] == SMALL)
    assert (small["samples"], small["not_compared"]) == (0, 1)
