"""API tests using a fake model; no model download required."""

import asyncio
import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app_assignment_1 import __version__, api

# How long the fake model takes to answer in the responsiveness test.
SLOW_PREDICT_SECONDS = 1.0


class FakeGenerator:
    def __init__(
        self, model_name: str, max_new_tokens: int, delay: float = 0.0
    ) -> None:
        self.model_name = model_name
        self.max_new_tokens = max_new_tokens
        self.delay = delay

    def generate(self, text: str) -> str:
        # A synchronous sleep stands in for CPU-bound inference.
        time.sleep(self.delay)
        return f"Test response for: {text}"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(api, "TextGenerator", FakeGenerator)

    with TestClient(api.app) as test_client:
        yield test_client


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "environment": api.settings.environment,
    }


def test_predict_valid_input(client: TestClient) -> None:
    response = client.post("/predict", json={"text": "hello"})

    assert response.status_code == 200
    assert response.json() == {
        "prediction": "Test response for: hello",
        "input_length": 5,
    }


def test_predict_rejects_empty_text(client: TestClient) -> None:
    response = client.post("/predict", json={"text": ""})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_health_answers_while_predict_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health must answer while a slow /predict is still running.

    Unlike the tests above, this one sends two requests at once, so it uses
    httpx.AsyncClient over ASGITransport instead of TestClient. ASGITransport
    does not run the app's lifespan, so the fake generator is placed on
    app.state directly. If concurrency tests grow, move them to their own file.
    """
    slow_generator = FakeGenerator(
        model_name="fake", max_new_tokens=1, delay=SLOW_PREDICT_SECONDS
    )
    monkeypatch.setattr(api.app.state, "generator", slow_generator, raising=False)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        predict_task = asyncio.create_task(
            client.post("/predict", json={"text": "slow"})
        )
        # Give /predict time to start. If it blocks the event loop, this
        # sleep cannot return until the slow request has finished.
        await asyncio.sleep(0.1)

        start = time.perf_counter()
        health_response = await client.get("/health")
        health_seconds = time.perf_counter() - start

        assert not predict_task.done(), "/predict finished before /health ran"
        predict_response = await predict_task

    assert health_response.status_code == 200
    assert predict_response.status_code == 200
    assert health_seconds < SLOW_PREDICT_SECONDS / 2
