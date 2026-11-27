"""scripts/measure_recovery.py: the measurements, and every scenario run
end to end against the app in process, with a 1-second cooldown."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_recovery.py"
TOKEN = "t" * 32


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_recovery", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up here to resolve postponed annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mr = load()


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {
        "backends_path": path,
        "operator_token": TOKEN,
        "controller_cooldown_seconds": 1,
        "semantic_actions_enabled": True,
        "semantic_baseline_disagreement_rate": 0.0,
    } | overrides
    return Settings(_env_file=None, **values)


def transport(client: TestClient):
    def send(method: str, path: str, payload: Any, headers: dict[str, str]):
        response = client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.json()

    return send


def reply(at: float, phase: str, **fields: Any):
    defaults = {
        "route": "standard",
        "status": 200,
        "model": mr.SMALL,
        "intent": "card_arrival",
        "format_check": "passed",
        "expected_intent": "card_arrival",
        "latency_ms": 10.0,
    }
    return mr.Reply(at=at, phase=phase, **(defaults | fields))


def test_summarize():
    disabled = f"disabled {mr.SMALL}: format failure rate 50% over last 10 calls"
    replies = [
        reply(0.0, "normal"),
        reply(0.5, "normal", route="complex", model=mr.LARGE),
        reply(1.0, "fault", format_check="failed", intent="unknown"),
        reply(1.5, "fault", route="complex", model=mr.LARGE, latency_ms=30.0),
        reply(
            2.0,
            "fault",
            format_check="failed",
            intent="unknown",
            controller_action=disabled,
        ),
        reply(2.5, "fault", model=mr.LARGE, latency_ms=40.0),
        reply(3.0, "cleared", model=mr.LARGE),
        reply(3.5, "cleared", status=503, model=None, intent=None, format_check=None),
        reply(5.0, "cleared"),
    ]
    result = mr.summarize("bad_format", replies, fault_at=0.8, cleared_at=2.8)
    assert result.detected
    assert result.detect_seconds == 1.2
    # Two standard requests up to and including the one that disabled it.
    assert result.requests_to_detect == 2
    assert result.bad_replies_before_detect == 2
    # Over the whole observation: the two before the disable and the 503.
    assert result.bad_replies == 3
    assert result.unavailable == 1
    assert result.false_interventions == 0
    assert result.restored
    # From when the fault was cleared.
    assert result.restore_seconds == 2.2
    assert result.latency_ms_normal == 10.0
    # Up to and including the disable; the reply after it is reported apart.
    assert result.latency_ms_fault == 10.0
    assert result.latency_ms_after_disable == 40.0


def test_a_controller_action_before_the_fault_is_a_false_intervention():
    replies = [
        reply(0.0, "normal", controller_action="disabled qwen3-4b: ..."),
        reply(1.0, "fault"),
    ]
    result = mr.summarize("outage", replies, fault_at=0.5)
    assert result.false_interventions == 1
    assert not result.detected
    assert not result.restored


@pytest.mark.parametrize("scenario", ["outage", "bad_format", "label_swap"])
def test_each_scenario_is_detected_and_recovers(tmp_path, scenario):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        gateway = mr.Gateway(transport(client), TOKEN)
        result = mr.run_scenario(gateway, scenario, interval=0, timeout=10)
        health = client.get("/v1/health").json()
    assert result.detected, scenario
    assert result.restored, scenario
    assert result.false_interventions == 0
    assert result.unavailable == 0
    # 1 s cooldown: the model serves again about a second after the fault ends.
    assert 0 < result.restore_seconds < 3
    if scenario == "outage":
        # The large model answers every request the small one fails.
        assert result.bad_replies_before_detect == 0
    else:
        assert result.bad_replies_before_detect > 0
    # Every fault is cleared afterwards.
    assert all(
        b["error_rate"] == b["fault_rate"] == b["label_swap_rate"] == 0
        for b in health["backends"]
    )


def test_the_next_scenario_waits_out_a_cooldown(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        gateway = mr.Gateway(transport(client), TOKEN)
        first = mr.run_scenario(gateway, "outage", interval=0, timeout=10)
        second = mr.run_scenario(gateway, "bad_format", interval=0, timeout=10)
    assert first.detected and second.detected
    assert second.false_interventions == 0


def test_without_the_token_the_run_fails_with_a_clear_error(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        gateway = mr.Gateway(transport(client), None)
        result = mr.run_scenario(gateway, "outage", interval=0, timeout=10)
    assert result.error and "APP_OPERATOR_TOKEN" in result.error
    assert result.detect_seconds is None


def test_label_swap_needs_semantic_actions_and_a_baseline(tmp_path):
    for overrides, ready in [
        ({}, True),
        ({"semantic_actions_enabled": False}, False),
        ({"semantic_baseline_disagreement_rate": None}, False),
    ]:
        with TestClient(create_app(settings(tmp_path, **overrides))) as client:
            assert mr.label_swap_ready(client.get("/v1/health").json()) is ready


def test_refuses_a_remote_host(capsys):
    assert mr.main(["http://gateway.example.org:8080"]) == 2
    assert "Refusing gateway.example.org" in capsys.readouterr().err


def test_an_undetected_fault_still_counts_its_bad_replies():
    # Audit of #104: one malformed reply and no disable reported 0.
    replies = [
        reply(0.0, "normal"),
        reply(1.0, "fault", format_check="failed", intent="unknown"),
        reply(1.5, "fault"),
        reply(2.5, "cleared"),
    ]
    result = mr.summarize("bad_format", replies, fault_at=0.8, cleared_at=2.0)
    assert not result.detected
    assert result.bad_replies_before_detect == 1
    assert result.bad_replies == 1
    assert not result.ok


def test_the_recovery_clock_starts_when_the_fault_is_cleared():
    # Audit of #104: the clock started when the first reply after the clear
    # arrived, so a slow first reply hid its own wait.
    disabled = f"disabled {mr.SMALL}: ..."
    replies = [
        reply(0.0, "normal"),
        reply(1.0, "fault", controller_action=disabled),
        reply(4.5, "cleared"),
    ]
    result = mr.summarize("outage", replies, fault_at=0.5, cleared_at=3.0)
    assert result.restore_seconds == 1.5


def test_a_false_intervention_fails_the_run():
    disabled = f"disabled {mr.SMALL}: ..."
    replies = [
        reply(0.0, "normal", controller_action=disabled),
        reply(1.0, "fault", controller_action=disabled),
        reply(2.5, "cleared"),
    ]
    result = mr.summarize("outage", replies, fault_at=0.5, cleared_at=2.0)
    assert result.detected and result.restored
    assert not result.ok


def test_each_run_starts_from_clean_windows(tmp_path):
    # Review of #104: an interrupted label_swap left 3 disagreements in the
    # semantic window, and the next run disabled a healthy model.
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        client.post(
            "/v1/faults", json={"model": mr.SMALL, "fault": "label_swap"}, headers=auth
        )
        for _ in range(3):
            client.post("/v1/chat", json={"message": mr.STANDARD[0]})
        client.post(
            "/v1/faults", json={"model": mr.SMALL, "fault": "none"}, headers=auth
        )
        gateway = mr.Gateway(transport(client), TOKEN)
        result = mr.run_scenario(gateway, "label_swap", interval=0, timeout=10)
    assert result.false_interventions == 0
    assert result.ok


def flaky(send, fail_on_chat: int):
    """A transport whose `fail_on_chat`-th chat raises, like a reset."""
    chats = 0

    def wrapped(method, path, payload, headers):
        nonlocal chats
        if path == "/v1/chat":
            chats += 1
            if chats == fail_on_chat:
                raise OSError("connection reset")
        return send(method, path, payload, headers)

    return wrapped


def test_a_failed_run_is_reported_and_the_others_kept(tmp_path, monkeypatch, capsys):
    # Review of #104: one failure threw away every run and the --out file.
    app = create_app(settings(tmp_path))
    out = tmp_path / "out.json"
    bases = []
    with TestClient(app) as client:

        def fake_transport(base, timeout=60):
            bases.append(base)
            return flaky(transport(client), fail_on_chat=10)

        monkeypatch.setattr(mr, "http_transport", fake_transport)
        monkeypatch.setenv("APP_OPERATOR_TOKEN", TOKEN)
        code = mr.main(
            [
                "http://127.0.0.1:8080/",
                "--scenario",
                "outage",
                "--runs",
                "2",
                "--interval",
                "0",
                "--out",
                str(out),
            ]
        )
        health = client.get("/v1/health").json()
    # A trailing slash on the URL is dropped.
    assert bases == ["http://127.0.0.1:8080"]
    assert code == 1
    results = json.loads(out.read_text())["results"]
    assert [r["error"] is None for r in results] == [False, True]
    assert results[0]["error"] == "OSError: connection reset"
    assert results[1]["ok"]
    assert "Run failed" in capsys.readouterr().out
    # The failed run still cleared its fault.
    assert all(b["error_rate"] == 0 for b in health["backends"])


def test_a_run_where_every_scenario_is_skipped_fails(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path, semantic_actions_enabled=False))
    with TestClient(app) as client:
        monkeypatch.setattr(mr, "http_transport", lambda base: transport(client))
        assert mr.main(["--scenario", "label_swap"]) == 1


def test_a_wrong_token_prints_the_message_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(mr, "http_transport", lambda base: transport(client))
        monkeypatch.setenv("APP_OPERATOR_TOKEN", "wrong")
        assert mr.main(["--scenario", "outage", "--interval", "0"]) == 1
    captured = capsys.readouterr()
    assert "Run failed:" in captured.out and "APP_OPERATOR_TOKEN" in captured.out
    assert "Traceback" not in captured.err
    # The report is still printed.
    assert "scenario" in captured.out and "FAILED" in captured.out


def test_sigterm_stops_like_ctrl_c():
    with pytest.raises(KeyboardInterrupt):
        mr._stop_on_sigterm(15, None)


def test_messages_go_out_once_per_interval():
    # Review of #104: --interval was a pause after each reply, so the period
    # was the interval plus the reply time.
    now = [0.0]
    sleeps: list[float] = []
    disabled = {"n": 0}

    def send(method, path, payload, headers):
        if path == "/v1/health":
            return 200, {"status": "ok"}
        if path != "/v1/chat":
            return 200, {}
        now[0] += 0.3  # each reply takes 0.3 s
        disabled["n"] += 1
        action = f"disabled {mr.SMALL}: x" if disabled["n"] == 10 else None
        return 200, {
            "intent": "card_arrival",
            "trace": {
                "model": mr.SMALL,
                "format_check": "passed",
                "fallback_reason": None,
                "controller_action": action,
                "latency_ms": 300.0,
            },
        }

    def sleep(seconds):
        sleeps.append(round(seconds, 6))
        now[0] += seconds

    gateway = mr.Gateway(send, TOKEN)
    mr.run_scenario(
        gateway, "outage", interval=0.5, timeout=10, clock=lambda: now[0], sleep=sleep
    )
    assert sleeps and set(sleeps) == {0.2}


def _failing_gateway(fail_on, calls):
    """A gateway whose send raises on the call `fail_on` names."""

    def send(method, path, payload, headers):
        calls.append((method, path))
        if fail_on(method, path, len(calls)):
            raise OSError("network blip")
        if path == "/v1/health":
            return 200, {"status": "ok"}
        if path == "/v1/chat":
            return 200, {
                "intent": "card_arrival",
                "trace": {
                    "model": mr.SMALL,
                    "format_check": "passed",
                    "fallback_reason": None,
                    "controller_action": None,
                    "latency_ms": 1.0,
                },
            }
        return 200, {}

    return mr.Gateway(send, TOKEN)


def test_a_stop_between_disable_and_enable_still_enables_the_model():
    calls = []
    gateway = _failing_gateway(lambda m, p, n: False, calls)
    real_send = gateway.send
    seen = []

    def send(method, path, payload, headers):
        out = real_send(method, path, payload, headers)
        seen.append(path)
        if path.endswith(f"{mr.SMALL}/disable"):
            raise KeyboardInterrupt  # SIGTERM right after the disable
        return out

    gateway.send = send
    with pytest.raises(KeyboardInterrupt):
        gateway.reset_windows()
    assert seen[-1].endswith(f"{mr.SMALL}/enable")


def test_a_failed_run_keeps_the_detection_time_from_when_the_fault_was_set():
    now = [0.0]
    calls = []
    chats = {"n": 0}

    def fail_on(method, path, n):
        if path == "/v1/chat":
            chats["n"] += 1
            return chats["n"] == 12  # a few replies into the fault phase
        return False

    gateway = _failing_gateway(fail_on, calls)
    real_send = gateway.send

    def send(method, path, payload, headers):
        now[0] += 0.5
        return real_send(method, path, payload, headers)

    gateway.send = send
    result = mr.run_scenario(
        gateway,
        "outage",
        interval=0,
        timeout=10,
        clock=lambda: now[0],
        sleep=lambda s: None,
    )
    assert result.error and "network blip" in result.error
    assert result.fault_at > 0  # not the 0.0 default


def test_a_setup_failure_is_reported_and_later_runs_still_happen(
    tmp_path, monkeypatch, capsys
):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        good = transport(client)
        state = {"fail": True}

        def send(method, path, payload, headers):
            if state["fail"] and path.startswith("/v1/faults"):
                state["fail"] = False
                raise OSError("connection reset")
            return good(method, path, payload, headers)

        monkeypatch.setattr(mr, "http_transport", lambda base: send)
        monkeypatch.setenv("APP_OPERATOR_TOKEN", TOKEN)
        out = tmp_path / "out.json"
        code = mr.main(
            [
                "--scenario",
                "outage",
                "--runs",
                "2",
                "--interval",
                "0",
                "--out",
                str(out),
            ]
        )
    assert code == 1
    results = json.loads(out.read_text())["results"]
    assert len(results) == 2
    assert results[0]["error"] == "OSError: connection reset"
    assert results[1]["ok"]
    assert "scenario" in capsys.readouterr().out  # the table was printed
