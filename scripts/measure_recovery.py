"""Measure how fast the gateway notices a fault and recovers, on a running stack.

For each scenario, the script:
0. starts from clean monitor windows: it waits out any cooldown, then
   disables and enables each model through the operator routes, which clears
   both models' windows, so each run is an independent sample;
1. sends normal traffic and counts any controller action (a false intervention);
2. sets a simulated fault on the small model with POST /v1/faults;
3. keeps sending traffic until the controller disables the model, recording
   what customers got meanwhile;
4. clears the fault and keeps sending until the small model answers its own
   route again.

Scenarios: `outage` (every call fails), `bad_format` (every reply fails the
format check) and `label_swap` (well-formed replies with the wrong intent; it
needs semantic actions on and a baseline, so it is skipped otherwise).

Traffic is one message sent every `--interval` seconds (or as soon as the
last reply arrives, if it took longer), from a fixed list: three
standard messages for the small model, then one complex message for the large
one, so the large model keeps answering. The messages are test sentences, and
the script stores no customer data.

Usage, from the repo root, with the stack started with an operator token in
env/.env (APP_OPERATOR_TOKEN) and, for label_swap, compose.measure.yml:

    docker compose -f compose.yml -f compose.measure.yml up -d --build --wait
    APP_OPERATOR_TOKEN=... python3 scripts/measure_recovery.py --out recovery.json

It changes the running gateway's models, so it refuses any host but localhost
unless --allow-remote is given. Never run it on the deployment testers use.
The times depend on the settings (shown at the start) and on the mock models'
simulated latency; they say nothing about real models.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
# Message, the route it belongs to, and the intent a healthy model answers.
STANDARD = ("My card has not arrived", "standard", "card_arrival")
COMPLEX = ("My card was stolen", "complex", "lost_or_stolen_card")
TRAFFIC = (STANDARD, STANDARD, STANDARD, COMPLEX)
SCENARIOS = ("outage", "bad_format", "label_swap")

# method, path, JSON body or None, headers -> HTTP status and JSON body
Transport = Callable[[str, str, Any, dict[str, str]], tuple[int, Any]]


def http_transport(base: str, timeout: float = 60) -> Transport:
    def send(
        method: str, path: str, payload: Any, headers: dict[str, str]
    ) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    return send


@dataclass
class Reply:
    """One chat request, as the customer saw it. `at` is seconds since the
    scenario started."""

    at: float
    phase: str  # normal, fault or cleared
    route: str
    status: int
    model: str | None = None
    intent: str | None = None
    format_check: str | None = None
    fallback_reason: str | None = None
    controller_action: str | None = None
    latency_ms: float | None = None
    expected_intent: str = ""

    @property
    def good(self) -> bool:
        """A 200 that passed the format check with the right intent."""
        return (
            self.status == 200
            and self.format_check == "passed"
            and self.intent == self.expected_intent
        )

    @property
    def disabled_small(self) -> bool:
        return (self.controller_action or "").startswith(f"disabled {SMALL}")


@dataclass
class Result:
    scenario: str
    fault_at: float
    cleared_at: float | None = None
    detected: bool = False
    detect_seconds: float | None = None
    requests_to_detect: int | None = None
    # Bad replies from the fault until the disable, or for the whole fault
    # phase if the controller never acted.
    bad_replies_before_detect: int = 0
    # Bad replies over the whole observation after the fault was set: the
    # fault phase and the time after it was cleared.
    bad_replies: int = 0
    unavailable: int = 0
    false_interventions: int = 0
    restored: bool = False
    restore_seconds: float | None = None
    latency_ms_normal: float | None = None
    # Fault phase up to and including the request that disabled the model,
    # then the requests after it (answered by the large model).
    latency_ms_fault: float | None = None
    latency_ms_after_disable: float | None = None
    # Why the run stopped early, if it did; its other fields are then partial.
    error: str | None = None
    replies: list[Reply] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Detected, recovered, and no false intervention or error."""
        return (
            self.detected
            and self.restored
            and self.false_interventions == 0
            and self.error is None
        )


def summarize(
    scenario: str,
    replies: list[Reply],
    fault_at: float,
    cleared_at: float | None = None,
) -> Result:
    """Work out the measurements from the replies of one scenario. `fault_at`
    and `cleared_at` are when the fault was set and cleared, in seconds since
    the scenario started; each reply's `at` is when it arrived."""
    result = Result(
        scenario=scenario, fault_at=fault_at, cleared_at=cleared_at, replies=replies
    )
    normal = [r for r in replies if r.phase == "normal"]
    fault = [r for r in replies if r.phase == "fault"]
    cleared = [r for r in replies if r.phase == "cleared"]
    result.false_interventions = sum(r.controller_action is not None for r in normal)
    result.unavailable = sum(r.status != 200 for r in fault + cleared)
    result.bad_replies = sum(not r.good for r in fault + cleared)
    detect = next((r for r in fault if r.disabled_small), None)
    before, after = fault, []
    if detect is not None:
        result.detected = True
        result.detect_seconds = round(detect.at - fault_at, 2)
        before = fault[: fault.index(detect) + 1]
        after = fault[fault.index(detect) + 1 :]
        result.requests_to_detect = sum(r.route == "standard" for r in before)
    result.bad_replies_before_detect = sum(not r.good for r in before)
    if cleared_at is not None:
        back = next((r for r in cleared if _small_serves(r)), None)
        if back is not None:
            result.restored = True
            # From the moment the fault was cleared, not from the first reply
            # after it, which may itself have been slow.
            result.restore_seconds = round(back.at - cleared_at, 2)
    result.latency_ms_normal = _median(normal)
    result.latency_ms_fault = _median(before)
    result.latency_ms_after_disable = _median(after)
    return result


def _small_serves(reply: Reply) -> bool:
    """The small model answered its own route with a good reply."""
    return reply.route == "standard" and reply.model == SMALL and reply.good


def _median(replies: list[Reply]) -> float | None:
    values = [r.latency_ms for r in replies if r.latency_ms is not None]
    return round(statistics.median(values), 1) if values else None


class Gateway:
    def __init__(self, send: Transport, token: str | None):
        self.send = send
        self.token = token
        self.sent = 0

    def _operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health(self) -> dict[str, Any]:
        status, body = self.send("GET", "/v1/health", None, {})
        if status != 200:
            raise RuntimeError(f"/v1/health answered {status}")
        return body

    def fault(self, model: str, fault: str) -> None:
        status, body = self.send(
            "POST", "/v1/faults", {"model": model, "fault": fault}, self._operator()
        )
        if status != 200:
            raise RuntimeError(
                f"/v1/faults answered {status}: {body}. Set APP_OPERATOR_TOKEN, "
                "the same value, on the gateway and here."
            )

    def reset_windows(self) -> None:
        """Clear both models' monitor windows: an operator disable then enable
        of each, one at a time so one model always serves."""
        for model in (SMALL, LARGE):
            for action in ("disable", "enable"):
                status, body = self.send(
                    "POST",
                    f"/v1/operator/models/{model}/{action}",
                    {"reason": "measure_recovery.py: clean windows for a run"},
                    self._operator(),
                )
                if status != 200:
                    raise RuntimeError(
                        f"/v1/operator/models/{model}/{action} answered {status}:"
                        f" {body}"
                    )

    def chat(self, phase: str, start: float, clock: Callable[[], float]) -> Reply:
        message, route, intent = TRAFFIC[self.sent % len(TRAFFIC)]
        self.sent += 1
        status, body = self.send("POST", "/v1/chat", {"message": message}, {})
        reply = Reply(
            at=round(clock() - start, 3),
            phase=phase,
            route=route,
            status=status,
            expected_intent=intent,
        )
        if status == 200:
            trace = body["trace"]
            reply.model = trace["model"]
            reply.intent = body["intent"]
            reply.format_check = trace["format_check"]
            reply.fallback_reason = trace["fallback_reason"]
            reply.controller_action = trace["controller_action"]
            reply.latency_ms = trace["latency_ms"]
        return reply


def run_scenario(
    gateway: Gateway,
    scenario: str,
    *,
    interval: float,
    normal_requests: int = 8,
    after_detect: int = 4,
    timeout: float = 180,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Run one scenario and return its measurements. Clears every fault and
    both monitor windows before it starts, and the fault after it ends. An
    error partway returns what was measured so far, with `error` set."""
    for model in (SMALL, LARGE):
        gateway.fault(model, "none")
    _wait_until_serving(gateway, interval, timeout, clock, sleep)
    gateway.reset_windows()
    gateway.sent = 0
    start = clock()
    replies: list[Reply] = []
    fault_at: float = 0.0
    cleared_at: float | None = None

    def tick(phase: str) -> Reply:
        sent = clock()
        reply = gateway.chat(phase, start, clock)
        replies.append(reply)
        # A message every `interval` seconds, not a pause after each reply.
        sleep(max(0.0, interval - (clock() - sent)))
        return reply

    try:
        fault_at, cleared_at = _run_phases(
            gateway,
            scenario,
            tick,
            start,
            after_detect,
            normal_requests,
            timeout,
            clock,
        )
    except Exception as exc:
        result = summarize(scenario, replies, fault_at, cleared_at)
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    return summarize(scenario, replies, fault_at, cleared_at)


def _run_phases(
    gateway: Gateway,
    scenario: str,
    tick: Callable[[str], Reply],
    start: float,
    after_detect: int,
    normal_requests: int,
    timeout: float,
    clock: Callable[[], float],
) -> tuple[float, float | None]:
    for _ in range(normal_requests):
        tick("normal")
    fault_at = round(clock() - start, 3)
    try:
        gateway.fault(SMALL, scenario)
        while clock() - start - fault_at < timeout:
            if tick("fault").disabled_small:
                break
        for _ in range(after_detect):
            tick("fault")
    finally:
        gateway.fault(SMALL, "none")
    cleared_at = round(clock() - start, 3)
    while clock() - start - cleared_at < timeout:
        if _small_serves(tick("cleared")):
            break
    return fault_at, cleared_at


def _wait_until_serving(
    gateway: Gateway,
    interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Wait out a cooldown or hold left from an earlier run. A re-enable
    happens on the next check, and /v1/health is a check."""
    give_up = clock() + timeout
    while True:
        health = gateway.health()
        if health["status"] == "ok":
            return
        if clock() > give_up:
            raise RuntimeError(
                "A model stayed out of service; enable it or check the config"
            )
        sleep(max(interval, 1.0))


def label_swap_ready(health: dict[str, Any]) -> bool:
    rules = health["semantic_rules"]
    return bool(rules["actions_enabled"]) and (
        rules["baseline_disagreement_rate"] is not None
    )


def print_report(results: list[Result], health: dict[str, Any]) -> None:
    rules = health["controller"]
    semantic = health["semantic_rules"]
    print(
        f"Controller: window {rules['window']}, "
        f"min samples {rules['min_samples']}, "
        f"threshold {rules['failure_threshold']}, "
        f"cooldown {rules['cooldown_seconds']} s"
    )
    print(
        f"Semantic: actions {'on' if semantic['actions_enabled'] else 'off'}, "
        f"baseline {semantic['baseline_disagreement_rate']}, "
        f"min samples {semantic['min_samples']}"
    )
    header = (
        "scenario     detect_s  requests  bad_before  bad_all  503s  false  "
        "restore_s  p50_ms normal/fault/after"
    )
    print(header)
    for r in results:
        print(
            f"{r.scenario:<12} {_cell(r.detect_seconds):>8}  "
            f"{_cell(r.requests_to_detect):>8}  {r.bad_replies_before_detect:>10}  "
            f"{r.bad_replies:>7}  {r.unavailable:>4}  {r.false_interventions:>5}  "
            f"{_cell(r.restore_seconds):>9}  "
            f"{_cell(r.latency_ms_normal)}/{_cell(r.latency_ms_fault)}/"
            f"{_cell(r.latency_ms_after_disable)}"
            + (f"  FAILED: {r.error}" if r.error else "")
        )


def _cell(value: float | int | None) -> str:
    return "-" if value is None else str(value)


def _stop_on_sigterm(signum: int, frame: Any) -> None:
    # Closing the terminal or a CI kill: stop as Ctrl-C does, so the
    # `finally` in run_scenario clears the fault.
    raise KeyboardInterrupt


def write_out(path: str, health: dict[str, Any], results: list[Result]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "controller": health["controller"],
                "semantic_rules": health["semantic_rules"],
                "results": [asdict(r) | {"ok": r.ok} for r in results],
            },
            f,
            indent=2,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8080")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario")
    parser.add_argument(
        "--interval", type=float, default=0.5, help="seconds between messages"
    )
    parser.add_argument(
        "--out", help="write every reply and result as JSON, after each run"
    )
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")
    host = urlparse(base_url).hostname
    if host not in ("localhost", "127.0.0.1", "::1") and not args.allow_remote:
        print(
            f"Refusing {host}: this script changes the running models. "
            "Pass --allow-remote for a test deployment no one else uses.",
            file=sys.stderr,
        )
        return 2
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    gateway = Gateway(http_transport(base_url), os.environ.get("APP_OPERATOR_TOKEN"))
    results: list[Result] = []
    try:
        health = gateway.health()
        scenarios = args.scenario or list(SCENARIOS)
        for scenario in scenarios:
            if scenario == "label_swap" and not label_swap_ready(health):
                print(
                    "Skipping label_swap: it needs "
                    "APP_SEMANTIC_ACTIONS_ENABLED=true and a baseline "
                    "(compose.measure.yml sets both)."
                )
                continue
            for run in range(args.runs):
                print(f"Running {scenario} ({run + 1} of {args.runs})...", flush=True)
                result = run_scenario(gateway, scenario, interval=args.interval)
                if result.error:
                    print(f"  Run failed: {result.error}", flush=True)
                results.append(result)
                if args.out:
                    write_out(args.out, health, results)
    except RuntimeError as exc:
        # A setup problem, such as a wrong token: the message says what to do.
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    print_report(results, health)
    if args.out:
        print(f"Wrote {args.out}")
    if not results:
        print("No scenario ran.", file=sys.stderr)
        return 1
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
