"""The end-to-end check script passes on the mocks and fails on a broken backend."""

import ast
import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.router import JevAnswer, MockJevRouter

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_check.py"


@pytest.fixture
def e2e():
    spec = importlib.util.spec_from_file_location("e2e_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings_for(tmp_path, **changes):
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
        backend.update(changes)
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


def serve(e2e, monkeypatch, client):
    def get_json(base, path, payload=None, timeout=90):
        response = (
            client.get(path) if payload is None else client.post(path, json=payload)
        )
        if response.status_code >= 400:
            raise HTTPError(path, response.status_code, "error", {}, None)
        return response.json()

    monkeypatch.setattr(e2e, "get_json", get_json)


def test_script_is_python_39_syntax():
    ast.parse(SCRIPT.read_text(), feature_version=(3, 9))


def test_passes_on_mocks(e2e, monkeypatch, tmp_path, capsys):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        results = e2e.run("http://test")
    out = capsys.readouterr().out
    assert results.failed == 0, out
    assert "MOCK Qwen" in out
    assert "SKIP: Prometheus counters" in out
    # The script never prints the fixed test messages either.
    assert all(message not in out for message, _, _ in e2e.MESSAGES)


def test_fails_when_replies_are_corrupted(e2e, monkeypatch, tmp_path):
    with TestClient(create_app(settings_for(tmp_path, fault_rate=1))) as client:
        serve(e2e, monkeypatch, client)
        results = e2e.run("http://test")
    assert results.failed > 0


def test_parse_metrics_sums_labels_and_drops_total_suffix(e2e):
    totals, series = e2e.parse_metrics(
        "# TYPE x counter\n"
        'gateway_chat_requests_total{route="a"} 2.0\n'
        'gateway_chat_requests_total{route="b"} 3.0\n'
    )
    assert totals["gateway_chat_requests"] == 5.0
    assert ("gateway_chat_requests", '{route="a"}') in series


def test_metrics_command_deltas(e2e, monkeypatch, tmp_path, capsys):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        metrics = client.get("/metrics").text
        monkeypatch.setattr(
            e2e,
            "read_metrics",
            lambda url, cmd: e2e.parse_metrics(client.get("/metrics").text),
        )
        results = e2e.run("http://test", metrics_cmd="unused")
    out = capsys.readouterr().out
    assert metrics
    assert results.failed == 0, out
    assert "Prometheus: gateway_chat_requests rose by 7 (sent 7;" in out


def test_outage_fails_even_when_run_is_labelled_real(e2e, monkeypatch, tmp_path):
    settings = settings_for(tmp_path)
    backends = json.loads(settings.backends_path.read_text())
    backends[0]["error_rate"] = 1
    settings.backends_path.write_text(json.dumps(backends))
    with TestClient(create_app(settings)) as client:
        serve(e2e, monkeypatch, client)
        plain = e2e.get_json

        def as_real(base, path, payload=None, timeout=90):
            body = plain(base, path, payload, timeout)
            if path == "/v1/health":
                body["mode"] = "real"
            return body

        monkeypatch.setattr(e2e, "get_json", as_real)
        results = e2e.run("http://test")
    assert results.failed > 0


def test_failed_final_metrics_scrape_is_reported_not_raised(
    e2e, monkeypatch, tmp_path, capsys
):
    calls = []

    def read_metrics(url, cmd):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("metrics command exited 1")
        return e2e.parse_metrics("")

    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        monkeypatch.setattr(e2e, "read_metrics", read_metrics)
        results = e2e.run("http://test", metrics_cmd="unused")
    assert results.failed == 1
    assert "metrics source reachable at the end" in capsys.readouterr().out


def test_final_health_failure_preserves_the_summary(e2e, monkeypatch, tmp_path, capsys):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        get_json = e2e.get_json
        health_reads = 0

        def fail_final_health(base, path, payload=None, timeout=90):
            nonlocal health_reads
            if path == "/v1/health":
                health_reads += 1
                if health_reads == 2:
                    raise OSError("gateway down")
            return get_json(base, path, payload, timeout)

        monkeypatch.setattr(e2e, "get_json", fail_final_health)
        assert e2e.main(["--base-url", "http://test"]) == 1
    out = capsys.readouterr().out
    assert "FAIL: gateway answers GET /v1/health at the end" in out
    assert "1 failed" in out


def test_jev_choice_cannot_mask_a_mismatched_serving_route(
    e2e, monkeypatch, tmp_path, capsys
):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        get_json = e2e.get_json

        def wrong_final_route(base, path, payload=None, timeout=90):
            body = get_json(base, path, payload, timeout)
            if path == "/v1/health":
                body["mode"] = "real"
            if path == "/v1/chat" or path.startswith("/v1/requests/"):
                if body["trace"]["route"] == "standard":
                    body["trace"]["route"] = "complex"
            return body

        monkeypatch.setattr(e2e, "get_json", wrong_final_route)
        assert e2e.run("http://test").failed > 0
    assert "FAIL: message 1: model serves the final route" in capsys.readouterr().out


def test_real_run_fails_if_it_never_exercises_small(e2e, monkeypatch, tmp_path, capsys):
    settings = settings_for(tmp_path)

    class AllComplex(MockJevRouter):
        async def route(self, message):
            result = await super().route(message)
            result.route = JevAnswer(
                choice="complex",
                confidence=1,
                probabilities={"standard": 0, "complex": 1},
            )
            return result

    with TestClient(create_app(settings, AllComplex(settings.intents_path))) as client:
        serve(e2e, monkeypatch, client)
        get_json = e2e.get_json

        def as_real(base, path, payload=None, timeout=90):
            body = get_json(base, path, payload, timeout)
            if path == "/v1/health":
                body["mode"] = "real"
            return body

        monkeypatch.setattr(e2e, "get_json", as_real)
        assert e2e.run("http://test").failed == 1
    assert "FAIL: coverage: qwen3-1.7b" in capsys.readouterr().out


def test_real_jev_run_fails_when_router_fell_back(e2e, monkeypatch, tmp_path, capsys):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        plain = e2e.get_json

        def as_real_jev(base, path, payload=None, timeout=90):
            body = plain(base, path, payload, timeout)
            if path == "/v1/health":
                body["jev_mode"] = "real"
            return body

        monkeypatch.setattr(e2e, "get_json", as_real_jev)
        results = e2e.run("http://test")
    out = capsys.readouterr().out
    # The mock router answered, so none of the seven replies came from real Jev.
    assert results.failed >= 7
    assert "routed by real Jev (router mock_jev" in out


def test_unbalanced_quote_in_metrics_cmd_is_a_fail_line(e2e, monkeypatch, tmp_path):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        serve(e2e, monkeypatch, client)
        results = e2e.run("http://test", metrics_cmd="echo 'oops")
    assert results.failed >= 1
