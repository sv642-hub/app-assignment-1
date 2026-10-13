"""API tests using a fake model; no model download required."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app_assignment_1 import __version__, api


class FakeGenerator:
    def __init__(self, model_name: str, max_new_tokens: int) -> None:
        self.model_name = model_name
        self.max_new_tokens = max_new_tokens

    def generate(self, text: str) -> str:
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
