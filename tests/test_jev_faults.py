"""Simulated Jev faults on command: POST /v1/faults/jev (scenarios F4 and F5 in
the chaos plan, #114)."""

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.jev import TypeSafeJevRouter, create_jev_http_client
from app_assignment_1.labels import load_labels

TOKEN = "operator-token-for-tests-only-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTE = "/v1/faults/jev"
MESSAGE = "my card has not arrived"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {"operator_token": TOKEN} | overrides
    return Settings(_env_file=None, backends_path=path, **values)  # type: ignore[arg-type]


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        yield client


def chat(client: TestClient) -> dict:
    response = client.post("/v1/chat", json={"message": MESSAGE})
    assert response.status_code == 200
    return response.json()["trace"]


def test_healthy_jev_routes_by_default(client):
    trace = chat(client)
    assert trace["router"] == "mock_jev"
    assert trace["fallback_reason"] is None


def test_outage_uses_the_rule_router_and_opens_the_breaker(client):
    response = client.post(ROUTE, json={"fault": "outage"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"error_rate": 1.0, "slow_rate": 0.0, "slow_seconds": 0.0}
    # Five failed calls open the breaker (the default); then Jev is skipped.
    reasons = [chat(client)["fallback_reason"] for _ in range(6)]
    assert reasons[:5] == ["jev_error"] * 5
    assert reasons[5] == "jev_circuit_open"
    assert client.get("/v1/health").json()["jev_breaker"]["state"] == "open"


def test_slow_jev_past_the_timeout_uses_the_rule_router(tmp_path):
    settings = make_settings(tmp_path, jev_timeout_seconds=0.2)
    with TestClient(create_app(settings)) as client:
        client.post(ROUTE, json={"fault": "slow", "seconds": 5}, headers=AUTH)
        start = time.perf_counter()
        trace = chat(client)
        elapsed = time.perf_counter() - start
    assert trace["router"] == "rule_router"
    assert trace["fallback_reason"] == "jev_timeout"
    assert elapsed < 2  # cut at the 0.2 s Jev timeout, not held for 5 s


def test_slow_jev_within_the_timeout_only_adds_latency(client):
    client.post(ROUTE, json={"fault": "slow", "seconds": 0.2}, headers=AUTH)
    start = time.perf_counter()
    trace = chat(client)
    assert time.perf_counter() - start >= 0.2
    assert trace["router"] == "mock_jev"
    assert trace["fallback_reason"] is None


def test_rate_zero_leaves_jev_answering(client):
    client.post(ROUTE, json={"fault": "outage", "rate": 0}, headers=AUTH)
    assert chat(client)["router"] == "mock_jev"


def test_none_clears_every_jev_fault(client):
    client.post(ROUTE, json={"fault": "outage"}, headers=AUTH)
    client.post(ROUTE, json={"fault": "slow", "seconds": 3}, headers=AUTH)
    response = client.post(ROUTE, json={"fault": "none"}, headers=AUTH)
    assert response.json() == {"error_rate": 0.0, "slow_rate": 0.0, "slow_seconds": 0.0}
    assert chat(client)["router"] == "mock_jev"


@pytest.mark.parametrize(
    "body",
    [
        {"fault": "slow"},  # seconds is required for slow
        {"fault": "outage", "seconds": 3},  # and only for slow
        {"fault": "none", "seconds": 3},
        {"fault": "slow", "seconds": 0},
        {"fault": "slow", "seconds": 121},
        {"fault": "outage", "rate": 1.5},
        {"fault": "outage", "rate": -0.1},
        {"fault": "bad_format"},  # backend faults stay on /v1/faults
        {"fault": "outage", "model": "jev"},
    ],
)
def test_bad_jev_fault_requests_are_rejected(client, body):
    assert client.post(ROUTE, json=body, headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        '{"fault": "outage", "rate": NaN}',
        '{"fault": "slow", "seconds": Infinity}',
    ],
)
def test_non_finite_numbers_are_a_422_not_a_500(client, body):
    response = client.post(
        ROUTE,
        content=body,
        headers=AUTH | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_the_backend_fault_route_also_answers_nan_with_a_422(client):
    # The 422 body echoes the input, and JSON can't hold NaN: this was a 500.
    response = client.post(
        "/v1/faults",
        content='{"model": "qwen3-1.7b", "fault": "outage", "rate": NaN}',
        headers=AUTH | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == "nan"


def test_jev_faults_need_the_operator_token(client, tmp_path):
    assert client.post(ROUTE, json={"fault": "outage"}).status_code == 401
    settings = make_settings(tmp_path, operator_token=None)
    with TestClient(create_app(settings)) as no_token:
        response = no_token.post(ROUTE, json={"fault": "outage"}, headers=AUTH)
    assert response.status_code == 403


def test_real_jev_cannot_be_faulted(tmp_path):
    settings = make_settings(tmp_path)
    http = create_jev_http_client(
        Settings(_env_file=None, jev_api_key="tsk-test"),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    router = TypeSafeJevRouter(http, "jev-1.14.0", load_labels(settings.labels_path))
    with TestClient(create_app(settings, router=router)) as client:
        response = client.post(ROUTE, json={"fault": "outage"}, headers=AUTH)
    assert response.status_code == 409
