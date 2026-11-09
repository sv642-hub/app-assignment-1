"""Failure monitor and controller: rules, gateway wiring and health."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.monitor import Controller


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_settings(
    tmp_path: Path, faults=(0, 0), errors=(0, 0), **overrides
) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend, fault, error in zip(backends, faults, errors, strict=True):
        backend["latency_ms"] = 0
        backend["fault_rate"] = fault
        backend["error_rate"] = error
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {
        "monitor_window": 4,
        "monitor_min_samples": 3,
        "monitor_failure_threshold": 0.5,
        "controller_cooldown_seconds": 30,
    } | overrides
    return Settings(_env_file=None, backends_path=path, **values)


def controller(tmp_path, clock=None) -> Controller:
    return Controller(
        ["qwen3-1.7b", "qwen3-4b"], make_settings(tmp_path), clock or Clock()
    )


def test_waits_for_minimum_samples(tmp_path):
    c = controller(tmp_path)
    assert c.record("qwen3-1.7b", True, can_disable=True) is None
    assert c.record("qwen3-1.7b", True, can_disable=True) is None
    action = c.record("qwen3-1.7b", True, can_disable=True)
    assert action == "disabled qwen3-1.7b: format failure rate 100% over last 3 calls"
    assert c.is_disabled("qwen3-1.7b")
    assert [i.action for i in c.log] == ["disabled"]


def test_rate_below_threshold_takes_no_action(tmp_path):
    c = controller(tmp_path)
    for failed in [True, False, False, False]:
        assert c.record("qwen3-1.7b", failed, can_disable=True) is None
    assert not c.is_disabled("qwen3-1.7b")
    stats = {s.name: s for s in c.stats()}["qwen3-1.7b"]
    assert (stats.samples, stats.format_failures, stats.failure_rate) == (4, 1, 0.25)


def test_window_forgets_old_failures(tmp_path):
    c = controller(tmp_path)
    for failed in [True, True, False, False, False, False]:
        c.record("qwen3-4b", failed, can_disable=False)
    stats = {s.name: s for s in c.stats()}["qwen3-4b"]
    assert (stats.samples, stats.format_failures) == (4, 0)


def test_cooldown_re_enables_with_a_fresh_window(tmp_path):
    clock = Clock()
    c = controller(tmp_path, clock)
    for _ in range(3):
        c.record("qwen3-1.7b", True, can_disable=True)
    clock.now = 29.9
    assert c.is_disabled("qwen3-1.7b")
    clock.now = 30.0
    assert not c.is_disabled("qwen3-1.7b")
    assert [i.action for i in c.log] == ["disabled", "re-enabled"]
    assert {s.name: s for s in c.stats()}["qwen3-1.7b"].samples == 0


def test_never_disables_the_last_usable_backend(tmp_path):
    c = controller(tmp_path)
    for _ in range(5):
        assert c.record("qwen3-4b", True, can_disable=False) is None
    assert not c.is_disabled("qwen3-4b")
    assert [i.action for i in c.log] == ["kept_last_backend"]


def test_min_samples_cannot_exceed_window(tmp_path):
    with pytest.raises(ValidationError, match="monitor_min_samples"):
        make_settings(tmp_path, monitor_window=2, monitor_min_samples=3)


def test_faulty_small_model_is_disabled_and_traffic_moves(tmp_path):
    settings = make_settings(tmp_path, faults=(1, 0))
    with TestClient(create_app(settings)) as client:
        traces = [
            client.post("/v1/chat", json={"message": "card arrival"}).json()["trace"]
            for _ in range(4)
        ]
        assert [t["format_check"] for t in traces[:3]] == ["failed"] * 3
        assert [t["controller_action"] for t in traces[:2]] == [None, None]
        assert traces[2]["controller_action"].startswith("disabled qwen3-1.7b")

        moved = traces[3]
        assert moved["jev_choice"] == "standard"
        assert moved["model"] == "qwen3-4b"
        assert moved["fallback_reason"] == "selected_backend_disabled"
        assert moved["format_check"] == "passed"

        health = client.get("/v1/health").json()
        assert health["status"] == "degraded"
        small = {m["name"]: m for m in health["monitor"]}["qwen3-1.7b"]
        assert small["disabled_by_controller"] is True
        assert 0 < small["disabled_for_seconds"] <= 30
        assert [i["action"] for i in health["interventions"]] == ["disabled"]


def test_both_faulty_keeps_serving_on_the_last_backend(tmp_path):
    settings = make_settings(tmp_path, faults=(1, 1))
    with TestClient(create_app(settings)) as client:
        for _ in range(6):
            response = client.post("/v1/chat", json={"message": "card arrival"})
            assert response.status_code == 200
        health = client.get("/v1/health").json()
        disabled = {m["name"]: m["disabled_by_controller"] for m in health["monitor"]}
        assert disabled == {"qwen3-1.7b": True, "qwen3-4b": False}
        assert [i["action"] for i in health["interventions"]] == [
            "disabled",
            "kept_last_backend",
        ]


def test_healthy_traffic_takes_no_action(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        for message in ["card arrival", "lost card", "weather on Mars"] * 2:
            trace = client.post("/v1/chat", json={"message": message}).json()["trace"]
            assert trace["controller_action"] is None
        health = client.get("/v1/health").json()
        assert health["status"] == "ok"
        assert health["interventions"] == []
        assert all(m["failure_rate"] == 0 for m in health["monitor"])


def test_format_failures_and_call_errors_share_one_rate(tmp_path):
    c = controller(tmp_path)
    assert c.record("qwen3-1.7b", True, can_disable=True) is None
    assert c.record("qwen3-1.7b", False, can_disable=True) is None
    action = c.record_error("qwen3-1.7b", can_disable=True)
    assert action == "disabled qwen3-1.7b: failure rate 67% over last 3 calls"
    stats = {s.name: s for s in c.stats()}["qwen3-1.7b"]
    assert (stats.format_failures, stats.call_errors) == (1, 1)
    assert stats.failure_rate == 0.67


async def failing_generate(message):
    raise TimeoutError("provider detail with a secret")


def test_failing_backend_falls_back_and_is_disabled(tmp_path, caplog):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        gateway = client.app.state.gateway
        small = next(b for b in gateway.backends if b.config.name == "qwen3-1.7b")
        small.generate = failing_generate
        responses = [
            client.post("/v1/chat", json={"message": "card arrival"}) for _ in range(4)
        ]
        assert [r.status_code for r in responses] == [200] * 4
        traces = [r.json()["trace"] for r in responses]
        assert [t["model"] for t in traces] == ["qwen3-4b"] * 4
        assert [t["fallback_reason"] for t in traces] == [
            "selected_backend_error",
            "selected_backend_error",
            "selected_backend_error",
            "selected_backend_disabled",
        ]
        assert [t["controller_action"] for t in traces[:2]] == [None, None]
        assert traces[2]["controller_action"] == (
            "disabled qwen3-1.7b: call error rate 100% over last 3 calls"
        )
        assert traces[3]["controller_action"] is None
        assert responses[0].json()["intent"] == "card_arrival"

        health = client.get("/v1/health").json()
        assert health["status"] == "degraded"
        monitors = {m["name"]: m for m in health["monitor"]}
        assert monitors["qwen3-1.7b"]["disabled_by_controller"] is True
        assert monitors["qwen3-1.7b"]["call_errors"] == 3
        assert monitors["qwen3-1.7b"]["format_failures"] == 0
        assert monitors["qwen3-1.7b"]["failure_rate"] == 1
        assert monitors["qwen3-4b"]["samples"] == 4
    assert "TimeoutError" in caplog.text
    assert "secret" not in caplog.text


def test_all_backends_failing_returns_503_and_keeps_the_last(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        gateway = client.app.state.gateway
        for backend in gateway.backends:
            backend.generate = failing_generate
        for _ in range(4):
            response = client.post("/v1/chat", json={"message": "card arrival"})
            assert response.status_code == 503
        health = client.get("/v1/health").json()
        assert health["status"] == "degraded"
        disabled = {m["name"]: m["disabled_by_controller"] for m in health["monitor"]}
        assert disabled == {"qwen3-1.7b": True, "qwen3-4b": False}
        assert [i["action"] for i in health["interventions"]] == [
            "disabled",
            "kept_last_backend",
        ]
        assert gateway.requests == {}


def test_shipped_chaos_config_is_valid():
    settings = Settings(
        _env_file=None, backends_path=Path("config/backends.chaos.json")
    )
    with TestClient(create_app(settings)) as client:
        faults = {
            b["name"]: b["fault_rate"]
            for b in client.get("/v1/health").json()["backends"]
        }
        assert faults == {"qwen3-1.7b": 1, "qwen3-4b": 0}


def test_outage_fault_moves_traffic_and_disables_the_model(tmp_path):
    settings = make_settings(tmp_path, errors=(1, 0))
    with TestClient(create_app(settings)) as client:
        traces = [
            client.post("/v1/chat", json={"message": "card arrival"}).json()["trace"]
            for _ in range(4)
        ]
        assert [t["model"] for t in traces] == ["qwen3-4b"] * 4
        assert [t["format_check"] for t in traces] == ["passed"] * 4
        assert traces[2]["controller_action"].startswith(
            "disabled qwen3-1.7b: call error rate"
        )
        assert traces[3]["fallback_reason"] == "selected_backend_disabled"
        monitors = {m["name"]: m for m in client.get("/v1/health").json()["monitor"]}
        assert monitors["qwen3-1.7b"]["call_errors"] == 3


def test_outage_fault_on_both_models_returns_503(tmp_path):
    settings = make_settings(tmp_path, errors=(1, 1))
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat", json={"message": "card arrival"})
        assert response.status_code == 503


def test_shipped_outage_config_is_valid():
    settings = Settings(
        _env_file=None, backends_path=Path("config/backends.outage.json")
    )
    with TestClient(create_app(settings)) as client:
        backends = client.get("/v1/health").json()["backends"]
    assert {b["name"]: b["error_rate"] for b in backends} == {
        "qwen3-1.7b": 1,
        "qwen3-4b": 0,
    }
    assert all(b["fault_rate"] == 0 for b in backends)


def test_re_enabled_entry_is_dated_when_the_cooldown_ended(tmp_path):
    clock = Clock()
    c = controller(tmp_path, clock)
    for _ in range(3):
        c.record("qwen3-1.7b", True, can_disable=True)
    clock.now = 100.0  # first checked 70 s after the 30 s cooldown ended
    checked_at = datetime.now(UTC)
    assert not c.is_disabled("qwen3-1.7b")
    _, re_enabled = c.log
    assert 69 < (checked_at - re_enabled.at).total_seconds() < 71


def test_kept_last_backend_is_logged_once_per_run_above_threshold(tmp_path):
    c = controller(tmp_path)
    for _ in range(3):
        c.record("qwen3-4b", True, can_disable=False)
    # Another backend's entry in between must not cause a repeat warning.
    for _ in range(3):
        c.record("qwen3-1.7b", True, can_disable=True)
    c.record("qwen3-4b", True, can_disable=False)
    assert [i.action for i in c.log] == ["kept_last_backend", "disabled"]
    # Dropping below the threshold ends the run; the next run warns again.
    for failed in [False, False, False, True, True, True]:
        c.record("qwen3-4b", failed, can_disable=False)
    assert [i.action for i in c.log] == [
        "kept_last_backend",
        "disabled",
        "kept_last_backend",
    ]


def test_health_reports_the_controller_rules(tmp_path):
    settings = make_settings(tmp_path, monitor_failure_threshold=0.4)
    with TestClient(create_app(settings)) as client:
        rules = client.get("/v1/health").json()["controller"]
    assert rules == {
        "window": 4,
        "min_samples": 3,
        "failure_threshold": 0.4,
        "cooldown_seconds": 30,
    }
