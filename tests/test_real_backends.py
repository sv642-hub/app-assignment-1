"""APP_PROVIDER_MODE=real: the gateway calls two fake vLLM servers."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1 import real_backends
from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.schemas import BackendReply
from app_assignment_1.service import backend_error_code
from app_assignment_1.vllm import (
    BackendCallError,
    BackendOutputError,
    BackendTimeoutError,
)

STANDARD = "my card has not arrived"  # mock Jev routes it to standard
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
KEY = "vllm-test-key"
TOKEN = "operator-token-for-tests-only-0123456789"


def reply(intent: str = "card_arrival", text: str | None = None) -> str:
    return json.dumps(
        {
            "intent": intent,
            "reply": "Your card should arrive within 5 business days."
            if text is None
            else text,
            "policy_facts": [],
        }
    )


class FakeVllm:
    """One fake vLLM server. Answers with `content`, a status, or an error."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.content = reply()
        self.error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        body = {"choices": [{"message": {"content": self.content}}]}
        return httpx.Response(self.status, json=body)


@pytest.fixture
def servers() -> dict[str, FakeVllm]:
    return {SMALL: FakeVllm(), LARGE: FakeVllm()}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(
        _env_file=None,
        backends_path=path,
        provider_mode="real",
        vllm_small_base_url="http://10.0.0.5:8001",
        vllm_large_base_url="http://10.0.0.5:8002",
        vllm_api_key=KEY,
        operator_token=TOKEN,
    )


@pytest.fixture
def clients(servers, monkeypatch) -> list[httpx.AsyncClient]:
    made: list[httpx.AsyncClient] = []
    real_create = real_backends.create_vllm_http_client
    by_port = {8001: servers[SMALL], 8002: servers[LARGE]}

    def client_for(settings: Settings, base_url: Any) -> httpx.AsyncClient:
        server = by_port[base_url.port]
        made.append(real_create(settings, base_url, httpx.MockTransport(server)))
        return made[-1]

    monkeypatch.setattr(real_backends, "create_vllm_http_client", client_for)
    return made


def chat(client: TestClient, message: str = STANDARD) -> dict[str, Any]:
    response = client.post("/v1/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def usage(client: TestClient) -> dict[str, dict[str, Any]]:
    return {u["name"]: u for u in client.get("/v1/health").json()["usage"]}


# --- wiring ---


def test_real_mode_calls_the_vllm_server_for_the_route(settings, servers, clients):
    with TestClient(create_app(settings)) as client:
        body = chat(client)
        health = client.get("/v1/health").json()
    trace = body["trace"]
    assert (trace["model"], trace["route"]) == (SMALL, "standard")
    assert body["reply"] == "Your card should arrive within 5 business days."
    assert trace["reply_source"] == "generated"
    assert trace["mocked"] is False
    assert trace["simulated_latency_ms"] == 0
    assert (health["mode"], health["jev_mode"]) == ("real", "mock")

    assert servers[LARGE].requests == []
    [request] = servers[SMALL].requests
    assert request.url == "http://10.0.0.5:8001/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    sent = json.loads(request.content)
    assert sent["model"] == SMALL
    assert sent["messages"][1] == {"role": "user", "content": STANDARD}
    assert len(clients) == 2 and all(c.is_closed for c in clients)


def test_complex_messages_go_to_the_large_server(settings, servers, clients):
    # lost_or_stolen_card is a complex intent in config/intents.json.
    with TestClient(create_app(settings)) as client:
        trace = chat(client, "My card was stolen yesterday")["trace"]
    assert (trace["jev_choice"], trace["model"]) == ("complex", LARGE)
    assert servers[SMALL].requests == []
    assert json.loads(servers[LARGE].requests[0].content)["model"] == LARGE
    assert servers[LARGE].requests[0].url.port == 8002


def test_unknown_and_uncarded_replies_keep_their_sources(settings, servers, clients):
    with TestClient(create_app(settings)) as client:
        servers[SMALL].content = reply("unknown", "Could you describe the problem?")
        assert chat(client)["trace"]["reply_source"] == "no_match"
        servers[SMALL].content = reply("activate_my_card", "")
        assert chat(client)["trace"]["reply_source"] == "unsupported_topic"


def test_faults_are_refused_on_a_real_model(settings, servers, clients):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/faults",
            json={"model": SMALL, "fault": "outage"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 409


def test_real_qwen_with_mock_jev_is_not_scored(settings, servers, clients):
    with TestClient(create_app(settings)) as client:
        chat(client)
        text = client.get("/metrics").text
    assert 'gateway_semantic_not_compared_total{model="qwen3-1.7b"} 1.0' in text


# --- error codes ---


@pytest.mark.parametrize(
    ("break_it", "code"),
    [
        (lambda s: setattr(s, "status", 500), "call_failed"),
        (lambda s: setattr(s, "error", httpx.ConnectError("down")), "call_failed"),
        (lambda s: setattr(s, "error", httpx.ReadTimeout("slow")), "timeout"),
        (lambda s: setattr(s, "content", "not json"), "invalid_output"),
        (
            lambda s: setattr(s, "content", '{"intent": "card_arrival"}'),
            "invalid_output",
        ),
    ],
    ids=["http_500", "connect_error", "read_timeout", "not_json", "missing_fields"],
)
def test_failures_get_an_error_code_and_fall_back(
    settings, servers, clients, break_it, code
):
    break_it(servers[SMALL])
    with TestClient(create_app(settings)) as client:
        trace = chat(client)["trace"]
        small = usage(client)[SMALL]
        text = client.get("/metrics").text
    assert (trace["model"], trace["fallback_reason"]) == (
        LARGE,
        "selected_backend_error",
    )
    assert small["failed_calls"] == 1
    assert small["failed_by_error"] == {code: 1}
    assert (
        f'gateway_backend_errors_total{{error="{code}",model="qwen3-1.7b"}} 1.0' in text
    )
    others = {"timeout", "call_failed", "invalid_output", "other"} - {code}
    for other in others:
        assert (
            f'gateway_backend_errors_total{{error="{other}",model="qwen3-1.7b"}} 0.0'
            in text
        )


def test_the_error_log_has_the_code_but_no_message(settings, servers, clients, caplog):
    servers[SMALL].status = 500
    with TestClient(create_app(settings)) as client:
        chat(client)
    lines = [r.getMessage() for r in caplog.records if "failed:" in r.getMessage()]
    assert lines == ["Backend qwen3-1.7b failed: call_failed (BackendCallError)"]
    assert STANDARD not in caplog.text


def test_error_codes_for_each_exception():
    try:
        BackendReply.model_validate({"intent": "x"})
    except ValidationError as exc:
        invalid = exc
    assert backend_error_code(BackendTimeoutError()) == "timeout"
    assert backend_error_code(TimeoutError()) == "timeout"  # the gateway's budget
    assert backend_error_code(BackendCallError()) == "call_failed"
    assert backend_error_code(ConnectionError()) == "call_failed"  # mock outage
    assert backend_error_code(BackendOutputError()) == "invalid_output"
    assert backend_error_code(invalid) == "invalid_output"
    assert backend_error_code(RuntimeError()) == "other"


def test_mock_mode_is_unchanged(tmp_path):
    with TestClient(create_app(Settings(_env_file=None))) as client:
        body = client.post("/v1/chat", json={"message": STANDARD}).json()
        health = client.get("/v1/health").json()
    assert body["trace"]["reply_source"] == "policy_template"
    assert body["trace"]["mocked"] is True
    assert health["mode"] == "mock"
    assert all(u["failed_by_error"] == {} for u in health["usage"])
