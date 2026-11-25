"""End-to-end check of a running gateway: routing, reply format, monitors, metrics.

Run against the public proxy (Python 3.9 or newer, standard library only):

    python3 scripts/e2e_check.py [--base-url http://localhost:8080]

It sends a small fixed set of messages to POST /v1/chat, then checks the
trace of each reply, the history route, the monitor and controller state on
GET /v1/health, and (when you give it a metrics source) the Prometheus
counters. It prints one PASS, FAIL or SKIP line per check and exits 1 if any
check failed.

Mock or real: the run is labelled from the gateway itself. While `mode` on
/v1/health is "mock" (or a reply trace says mocked), the expected intent and
model of each message are asserted, because the mocks are deterministic. When
the gateway uses real Qwen backends, those expectations come from the mock
keywords and a real model may differ, so they are only counted and reported,
never failed. Format checks, routing consistency, history, monitors and
metrics are checked in both modes.

/metrics is internal (Caddy does not proxy it). To include the Prometheus
checks, point --metrics-url at the gateway directly, or pass --metrics-cmd, a
command that prints the metrics text; the script runs it before and after the
messages (see docs/E2E_CHECK.md).

The script never prints or stores message text; the fixed messages below are
test inputs, not customer data.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# (message, expected intent, expected route). Expected values come from the
# keywords in config/intents.json, so they describe the mock Jev and Qwen.
MESSAGES = [
    ("My card has not arrived", "card_arrival", "standard"),
    ("What is the exchange rate?", "exchange_rate", "standard"),
    ("I want to verify my identity", "verify_my_identity", "standard"),
    ("I lost my card", "lost_or_stolen_card", "complex"),
    ("My top up failed", "top_up_failed", "complex"),
    ("My refund not showing in my account", "refund_not_showing_up", "complex"),
    ("What is the weather?", "unknown", "standard"),
]

BACKEND_MOVEMENT_REASONS = {
    "selected_backend_disabled",
    "selected_backend_error",
    "selected_backend_unavailable",
    "semantic_last_resort",
}

METRIC_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")


class Results:
    def __init__(self):
        self.failed = 0
        self.passed = 0
        self.skipped = 0

    def check(self, ok, label, detail=""):
        status = "PASS" if ok else "FAIL"
        self.passed += bool(ok)
        self.failed += not ok
        print(f"{status}: {label}" + (f" ({detail})" if detail else ""))
        return ok

    def skip(self, label, why):
        self.skipped += 1
        print(f"SKIP: {label} ({why})")


def get_json(base, path, payload=None, timeout=90):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def parse_metrics(text):
    """Sum every sample by metric name, ignoring labels and the _total suffix."""
    totals = {}
    by_label = {}
    for line in text.splitlines():
        match = METRIC_LINE.match(line)
        if not match or line.startswith("#"):
            continue
        name, labels, value = match.groups()
        name = name[: -len("_total")] if name.endswith("_total") else name
        try:
            number = float(value)
        except ValueError:
            continue
        totals[name] = totals.get(name, 0.0) + number
        by_label[(name, labels or "")] = number
    return totals, by_label


def read_metrics(url=None, command=None):
    if command:
        done = subprocess.run(
            shlex.split(command), capture_output=True, text=True, timeout=60
        )
        if done.returncode != 0:
            raise OSError("metrics command exited " + str(done.returncode))
        return parse_metrics(done.stdout)
    with urlopen(url, timeout=15) as response:
        return parse_metrics(response.read().decode())


def usage_by_model(health):
    return {item["name"]: item for item in health["usage"]}


def monitor_by_model(health):
    return {item["name"]: item for item in health["monitor"]}


def run(base, metrics_url=None, metrics_cmd=None):
    results = Results()

    try:
        before = get_json(base, "/v1/health")
    except (URLError, HTTPError, OSError, ValueError) as exc:
        results.check(False, "gateway answers GET /v1/health", type(exc).__name__)
        return results
    results.check(True, "gateway answers GET /v1/health", before["status"])

    mocked_qwen = before.get("mode", "mock") == "mock"
    jev_mode = before.get("jev_mode", "mock")
    label = "MOCK Qwen" if mocked_qwen else "REAL Qwen"
    print(f"Run label: {label}, Jev {jev_mode}, router {before.get('router')}")
    print(
        "Expected intents and models are asserted only against the mocks; with "
        "real backends they are reported, not failed."
        if mocked_qwen
        else "Real backends: intent and model agreement is measured, not asserted."
    )
    model_for_route = {item["route"]: item["name"] for item in before["backends"]}
    metrics_start = None
    if metrics_url or metrics_cmd:
        try:
            metrics_start = read_metrics(metrics_url, metrics_cmd)
        except (URLError, OSError, ValueError, subprocess.SubprocessError) as exc:
            results.check(False, "metrics source reachable", type(exc).__name__)

    sent = {}
    agreements = 0
    route_agreements = 0
    latencies = []
    for number, (message, intent, route) in enumerate(MESSAGES, 1):
        name = f"message {number}"
        started = time.time()
        try:
            result = get_json(base, "/v1/chat", {"message": message})
        except HTTPError as exc:
            results.check(False, f"{name}: POST /v1/chat", f"HTTP {exc.code}")
            continue
        except (URLError, OSError, ValueError) as exc:
            results.check(False, f"{name}: POST /v1/chat", type(exc).__name__)
            continue
        elapsed = time.time() - started
        trace = result["trace"]
        latencies.append(trace["latency_ms"])
        model = trace["model"]
        sent[model] = sent.get(model, 0) + 1
        agreements += result["intent"] == intent
        route_agreements += trace["route"] == route

        results.check(
            trace["format_check"] == "passed" and bool(result["reply"].strip()),
            f"{name}: reply passes the format check",
            ", ".join(trace.get("format_errors") or []),
        )
        # The final route must match the serving model, even after a move.
        results.check(
            model == model_for_route.get(trace["route"]),
            f"{name}: model serves the final route {trace['route']}",
            model,
        )
        first_route = trace.get("jev_choice")
        if first_route and first_route != trace["route"]:
            reasons = set((trace.get("fallback_reason") or "").split(", "))
            results.check(
                bool(reasons & BACKEND_MOVEMENT_REASONS),
                f"{name}: movement from {first_route} has a backend fallback reason",
            )
        if jev_mode == "real":
            # A rule-router answer means Jev failed, gave an invalid route or
            # the breaker skipped it: the run did not exercise real Jev.
            results.check(
                trace["router"] == "jev",
                f"{name}: routed by real Jev",
                f"router {trace['router']}, fallback reason: "
                f"{trace.get('fallback_reason')}"
                if trace["router"] != "jev"
                else "",
            )
        results.check(
            all(fact in result["reply"] for fact in result["policy_facts"]),
            f"{name}: reply contains its policy facts",
        )
        try:
            same = get_json(base, "/v1/requests/" + result["id"]) == result
        except (URLError, HTTPError, OSError, ValueError):
            same = False
        results.check(same, f"{name}: GET /v1/requests/{{id}} returns the same reply")
        if mocked_qwen and trace.get("router") == "mock_jev":
            results.check(
                result["intent"] == intent and trace["route"] == route,
                f"{name}: mock routing as expected ({route})",
                f"got intent {result['intent']}, route {trace['route']}",
            )
        print(f"  {name}: {elapsed * 1000:.0f} ms round trip, router {trace['router']}")

    total = len(MESSAGES)
    if not mocked_qwen or jev_mode == "real":
        print(
            f"MEASURED ({label}, Jev {jev_mode}): intent matched the keyword "
            f"expectation {agreements}/{total}, route {route_agreements}/{total}"
        )

    try:
        after = get_json(base, "/v1/health")
    except (URLError, OSError, ValueError) as exc:
        results.check(
            False, "gateway answers GET /v1/health at the end", type(exc).__name__
        )
        return results
    delivered = sum(sent.values())
    used_before, used_after = usage_by_model(before), usage_by_model(after)
    for model in sorted(set(model_for_route.values())):
        count = sent.get(model, 0)
        if not results.check(
            count > 0, f"coverage: {model} served at least one check message"
        ):
            continue
        gained = (
            used_after[model]["completed_calls"] - used_before[model]["completed_calls"]
        )
        results.check(
            gained >= count,
            f"health usage: {model} completed calls rose by {gained} (sent {count})",
        )
    mon_before, mon_after = monitor_by_model(before), monitor_by_model(after)
    for model in sorted(sent):
        grew = mon_after[model]["samples"] > mon_before[model]["samples"]
        full = mon_after[model]["samples"] >= after["controller"]["window"]
        results.check(
            grew or full,
            f"monitor: {model} window has {mon_after[model]['samples']} samples",
        )
    for model in sorted(used_after):
        failed = used_after[model]["failed_calls"] - used_before[model]["failed_calls"]
        results.check(
            failed <= 0,
            f"health usage: {model} had no failed calls during the run",
            f"{failed} failed" if failed > 0 else "",
        )
    stuck = [m for m, s in mon_after.items() if s["disabled_by_controller"]]
    results.check(not stuck, "controller: no model disabled", ", ".join(stuck))

    # /v1/health lists only the latest interventions, so compare entries, not
    # list lengths, which stop changing once the list is full.
    def keys(health):
        return {(i["at"], i["backend"], i["action"]) for i in health["interventions"]}

    new_actions = keys(after) - keys(before)
    results.check(
        not new_actions, "controller: no new interventions", str(len(new_actions))
    )
    results.check(after["status"] == "ok", "health status is ok", after["status"])
    breaker = after.get("jev_breaker", {})
    if jev_mode == "real":
        results.check(
            breaker.get("state", "closed") == "closed",
            "Jev circuit breaker is closed",
            str(breaker.get("state")),
        )
    semantic = [item for item in after["semantic"] if item["samples"]]
    if semantic:
        for item in semantic:
            print(
                f"  semantic {item['name']}: {item['samples']} scored, "
                f"status {item['status']}, disagreement {item['disagreement_rate']}"
            )
    else:
        print("  semantic monitor: no scored samples yet (collecting)")

    metrics_end = None
    if metrics_start:
        try:
            metrics_end = read_metrics(metrics_url, metrics_cmd)
        except (URLError, OSError, ValueError, subprocess.SubprocessError) as exc:
            results.check(
                False, "metrics source reachable at the end", type(exc).__name__
            )
    if metrics_start and metrics_end:
        gained = metrics_end[0].get("gateway_chat_requests", 0) - metrics_start[0].get(
            "gateway_chat_requests", 0
        )
        results.check(
            gained >= delivered,
            f"Prometheus: gateway_chat_requests rose by {gained:g} (sent {delivered}; "
            "more means other traffic)",
        )
        completed = metrics_end[0].get("gateway_backend_calls_completed", 0) - (
            metrics_start[0].get("gateway_backend_calls_completed", 0)
        )
        results.check(
            completed >= delivered,
            f"Prometheus: gateway_backend_calls_completed rose by {completed:g}",
        )
        for series in (
            "gateway_model_disabled",
            "gateway_monitor_failure_rate",
            "gateway_monitor_window_samples",
        ):
            results.check(
                any(key[0] == series for key in metrics_end[1]),
                f"Prometheus: {series} is exported",
            )
    elif not (metrics_url or metrics_cmd):
        results.skip("Prometheus counters", "no --metrics-url or --metrics-cmd given")

    if latencies:
        ordered = sorted(latencies)
        print(
            f"Gateway latency over {len(ordered)} replies: median "
            f"{ordered[len(ordered) // 2]:.0f} ms, max {ordered[-1]:.0f} ms ({label})"
        )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="End-to-end check of a gateway.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--metrics-url", help="gateway /metrics, for example :8000")
    parser.add_argument(
        "--metrics-cmd", help="command that prints the gateway /metrics text"
    )
    args = parser.parse_args(argv)
    results = run(args.base_url.rstrip("/"), args.metrics_url, args.metrics_cmd)
    print(
        f"\n{results.passed} passed, {results.failed} failed, {results.skipped} skipped"
    )
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
