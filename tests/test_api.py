"""Tests for the FastAPI service endpoints."""

from fastapi.testclient import TestClient

from app_assignment_1 import __version__
from app_assignment_1.api import app

client = TestClient(app)


def test_health_ok():
    """/health returns a typed, 200 payload confirming the service is up."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "environment": "local",
    }


def test_predict_valid_input():
    """/predict echoes validated input and reports its length."""
    response = client.post("/predict", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json() == {"prediction": "echo: hello", "input_length": 5}


def test_predict_rejects_empty_text():
    """Empty text fails validation at the edge with HTTP 422."""
    response = client.post("/predict", json={"text": ""})
    assert response.status_code == 422
