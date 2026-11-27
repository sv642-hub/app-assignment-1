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


# Groups A-E of docs/CHAOS_SCENARIOS.md, each run end to end in process with a
# 1-second cooldown. The long ones are shortened; the rules they check are not.
SHORT = {
    "A1": {"fault_seconds": 1.0},
    "E3": {"flap": (0.4, 0.4, 3.0)},
    "E4": {"quiet_seconds": 0.5},
}


def run_named(tmp_path, name, **overrides):
    from dataclasses import replace

    spec = replace(mr.CATALOG[name], **(SHORT.get(name, {}) | overrides))
    with TestClient(create_app(settings(tmp_path))) as client:
        gateway = mr.Gateway(transport(client), TOKEN)
        result = mr.run_scenario(gateway, spec, interval=0, timeout=10)
        health = client.get("/v1/health").json()
    # Every fault is cleared and no model is left on hold.
    assert all(
        b["error_rate"] == b["fault_rate"] == b["label_swap_rate"] == 0
        and b["available"]
        for b in health["backends"]
    )
    assert result.error is None, result.error
    assert result.false_interventions == 0
    return result


def test_the_catalog_has_groups_a_to_e():
    assert mr.GROUPS == ["A", "B", "C", "D", "E"]
    assert len(mr.CATALOG) == 20
    assert mr.chosen(None, None) == ["B1", "B2", "B3"]
    assert mr.chosen(["outage", "D4"], ["E"]) == ["B1", "D4", "E1", "E2", "E3", "E4"]


def test_a1_healthy_traffic_has_no_intervention(tmp_path):
    result = run_named(tmp_path, "A1")
    assert result.ok and not result.detected
    assert not [i for i in result.interventions if i["action"] == "disabled"]


def test_a2_no_intervention_after_the_recovery(tmp_path):
    result = run_named(tmp_path, "A2")
    assert result.ok and result.detected and result.restored
    assert any(r.phase == "after" for r in result.replies)


@pytest.mark.parametrize("name", ["B4", "B5"])
def test_b4_b5_the_large_model_is_disabled_and_recovers(tmp_path, name):
    result = run_named(tmp_path, name)
    assert result.ok
    assert result.disabled_model == mr.LARGE
    # 6 complex requests: the normal phase left 6 good calls in the window,
    # so it takes 6 failures to reach 50%, not the 5 an empty window needs.
    assert result.requests_to_detect == 6
    if name == "B4":
        # The small model answers the complex messages the large one fails.
        assert result.bad_replies_before_detect == 0


@pytest.mark.parametrize("name", ["C1", "C2", "C3", "C4", "C5"])
def test_partial_faults_run_and_report_an_outcome(tmp_path, name):
    result = run_named(tmp_path, name)
    assert result.ok  # measure only: the outcome is random
    faulted = [r for r in result.replies if r.phase == "fault"]
    assert len(faulted) == 60
    if result.detected:
        assert result.disabled_model == mr.SMALL and result.restored


@pytest.mark.parametrize("name", ["D1", "D2", "D3"])
def test_both_faulted_disables_one_and_keeps_the_other(tmp_path, name):
    result = run_named(tmp_path, name)
    assert result.ok, result.problems
    other = mr.LARGE if result.disabled_model == mr.SMALL else mr.SMALL
    assert result.kept_last == [other]
    assert result.restored  # both serve again once the faults clear
    if name == "D1":
        assert result.unavailable > 0  # neither model answers
    if name == "D2":
        assert result.unavailable == 0  # apologies, not 503s


def test_d4_an_operator_hold_makes_the_faulted_model_the_last_one(tmp_path):
    result = run_named(tmp_path, "D4")
    assert result.ok, result.problems
    assert not result.detected
    assert result.kept_last == [mr.SMALL]
    # The large model is held and the small one fails: every request a 503.
    faulted = [r for r in result.replies if r.phase == "fault"]
    assert all(r.status == 503 for r in faulted)
    # Found by this scenario: once the hold is released, the small model is
    # disabled on the outage failures still in its window, although the fault
    # is gone. Reported, not counted as a problem.
    late = result.extra["disabled_after_clear"]
    assert [x["model"] for x in late] == [mr.SMALL]


def test_e1_the_fault_returns_and_is_detected_again(tmp_path):
    result = run_named(tmp_path, "E1")
    assert result.ok, result.problems
    second = result.extra["second_cycle"]
    # 4, not 5: the reply that showed the model back is a good call in the
    # fresh window, and 1 good + 4 failures reaches the 5-call minimum at 80%.
    assert second["detected"] and second["requests_to_detect"] == 4
    # The second disable is the fault coming back, not a disable after the
    # clear (first stack run, 2026-11-27, reported it as one).
    assert "disabled_after_clear" not in result.extra


def test_e2_clearing_during_the_cooldown_does_not_end_it_early(tmp_path):
    result = run_named(tmp_path, "E2")
    assert result.ok
    # Cleared 1/6 of the 1 s cooldown after the disable: back only when the
    # cooldown ends, so most of a second later, not at once.
    assert 0.5 < result.restore_seconds < 1.5


def test_e3_flapping_reports_disables_and_time_out(tmp_path):
    result = run_named(tmp_path, "E3")
    assert result.ok
    small = result.extra["flapping"][mr.SMALL]
    assert small["disables"] >= 1 and small["seconds_out"] > 0
    assert small["seconds_out"] <= 3.0 + 0.5 and small["share_out"] <= 1


def test_flapping_time_out_counts_only_the_flapping_window():
    # First stack run, 2026-11-27: 5 cooldowns of 60 s were summed in full,
    # including time after the flapping ended, and showed 100%.
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)

    def at(seconds):
        return (t0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    run = mr._Run(None, mr.CATALOG["E3"], 0.5, 180, lambda: 0.0, lambda s: None)
    run.wall_start = t0
    run.marks = {"flap_start": 10.0, "flap_end": 70.0}
    log = [
        {"at": at(15), "backend": mr.SMALL, "action": "disabled"},
        {"at": at(35), "backend": mr.SMALL, "action": "re-enabled"},
        {"at": at(50), "backend": mr.SMALL, "action": "disabled"},
        {"at": at(110), "backend": mr.SMALL, "action": "re-enabled"},
    ]
    small = mr._flap_measures(log, mr.CATALOG["E3"], run)["flapping"][mr.SMALL]
    # 15-35 and 50-70 (cut at the end of the flapping): 40 s of 60.
    assert small == {"disables": 2, "seconds_out": 40.0, "share_out": 0.667}


def test_e4_no_disable_while_no_traffic_reaches_the_model(tmp_path):
    result = run_named(tmp_path, "E4")
    assert result.ok, result.problems
    assert not any(r.route == "standard" for r in result.replies if r.phase == "quiet")
    assert result.requests_to_detect == 6  # 6 good calls from the normal phase
    assert result.extra["detect_seconds_after_quiet"] >= 0


def test_a_disable_under_an_operator_hold_is_a_problem():
    disabled = f"disabled {mr.SMALL}: ..."
    replies = [reply(0.0, "normal"), reply(1.0, "fault", controller_action=disabled)]
    result = mr.summarize("D4", replies, 0.5, target=mr.SMALL, expect="kept_last")
    assert result.detected  # summarize reports it; run_scenario adds the problem
    assert result.problems == []


def test_a_disable_of_a_model_with_no_fault_is_false():
    disabled = f"disabled {mr.LARGE}: ..."
    replies = [reply(0.0, "normal"), reply(1.0, "fault", controller_action=disabled)]
    result = mr.summarize("B2", replies, 0.5)
    assert result.false_interventions == 1
    assert not result.ok
