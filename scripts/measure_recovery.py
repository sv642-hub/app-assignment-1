"""Measure how fast the gateway notices a fault and recovers, on a running stack.

Runs the scenarios in groups A to E of docs/CHAOS_SCENARIOS.md. For each run of
a scenario, the script:
0. starts from clean monitor windows: it waits out any cooldown, then
   disables and enables each model through the operator routes, which clears
   both models' windows, so each run is an independent sample;
1. sends normal traffic and counts any controller action (a false intervention);
2. sets the scenario's simulated faults with POST /v1/faults (and, for D4,
   holds the large model with an operator disable);
3. keeps sending traffic until the controller disables a faulted model, or for
   a fixed number of requests where the outcome is random or no disable is
   expected, recording what customers got meanwhile;
4. clears the faults and keeps sending until every faulted model answers its
   own route again.

Scenarios (see `CATALOG`; the doc has what each one expects):
- A1 healthy traffic, A2 healthy traffic after a recovery;
- B1-B5 one model, the full fault: outage, bad_format, label_swap on the small
  model, then outage and bad_format on the large one;
- C1-C5 partial faults at 10-70%, where the 50% threshold matters;
- D1-D4 both models faulted, and the last usable model;
- E1 the fault returns after recovery, E2 the fault cleared during the
  cooldown, E3 flapping, E4 a fault with no traffic to the faulted model.
`outage`, `bad_format` and `label_swap` still name B1, B2 and B3. label_swap
(B3) needs semantic actions on and a baseline, so it is skipped otherwise.

Kept-last-model decisions and re-enables are not on a reply's trace, so each
run also reads the interventions /v1/health lists, before and after.

Traffic is one message sent every `--interval` seconds (or as soon as the
last reply arrives, if it took longer), from a fixed list: three
standard messages for the small model, then one complex message for the large
one, so the large model keeps answering (the other way round when the large
model is faulted). The messages are test sentences, and the script stores no
customer data.

Usage, from the repo root, with the stack started with an operator token in
env/.env (APP_OPERATOR_TOKEN) and, for label_swap, compose.measure.yml:

    docker compose -f compose.yml -f compose.measure.yml up -d --build --wait
    APP_OPERATOR_TOKEN=... python3 scripts/measure_recovery.py --out recovery.json
    APP_OPERATOR_TOKEN=... python3 scripts/measure_recovery.py --group C --runs 10

With no --scenario or --group it runs B1-B3, as before. A1 takes 10 minutes,
E3 5 minutes and E4 2 minutes per run, on top of the cooldowns.

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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
# Message, the route it belongs to, and the intent a healthy model answers.
Message = tuple[str, str, str]
STANDARD: Message = ("My card has not arrived", "standard", "card_arrival")
COMPLEX: Message = ("My card was stolen", "complex", "lost_or_stolen_card")
TRAFFIC = (STANDARD, STANDARD, STANDARD, COMPLEX)
# For a fault on the large model: mostly its own route.
COMPLEX_TRAFFIC = (COMPLEX, COMPLEX, COMPLEX, STANDARD)
ROUTE = {SMALL: "standard", LARGE: "complex"}

# method, path, JSON body or None, headers -> HTTP status and JSON body
Transport = Callable[[str, str, Any, dict[str, str]], tuple[int, Any]]


@dataclass(frozen=True)
class Fault:
    model: str
    kind: str  # a FaultKind: outage, bad_format, label_swap
    rate: float = 1.0


@dataclass(frozen=True)
class Scenario:
    """One row of docs/CHAOS_SCENARIOS.md, as the script runs it.

    expect is what a run should show:
    - disable: `target` (or, without one, a faulted model) is disabled, then
      serves again once the faults clear;
    - no_disable: no model is disabled;
    - kept_last: the controller logs kept_last_backend, because the other
      model is not usable;
    - measure: no expectation; the outcome is random (partial faults), so
      several runs give a rate.
    """

    name: str
    title: str
    faults: tuple[Fault, ...] = ()
    target: str | None = SMALL
    expect: str = "disable"
    traffic: tuple[Message, ...] = TRAFFIC
    # A fault phase of fixed length, in requests or seconds, instead of "until
    # the disable". For random outcomes, or where no disable is expected.
    fault_requests: int | None = None
    fault_seconds: float | None = None
    # Requests sent after the disable, before the faults clear.
    after_detect: int = 4
    # E2: keep the fault on for this share of the cooldown after the disable.
    clear_after_cooldowns: float = 0.0
    # D4: an operator holds this model disabled while the faults are on.
    hold: str | None = None
    # E1: set the same faults again once the model is back.
    cycles: int = 1
    # A2: healthy requests after the recovery; any controller action is false.
    after_requests: int = 0
    # E4: send only the other model's route for this long once the fault is on.
    quiet_seconds: float = 0.0
    # E3: the faults go on and off: seconds on, seconds off, seconds in all.
    flap: tuple[float, float, float] | None = None
    # Needs semantic actions and a baseline (label_swap).
    semantic: bool = False

    @property
    def faulted(self) -> set[str]:
        return {f.model for f in self.faults}


def _one(name: str, title: str, model: str, kind: str, **more: Any) -> Scenario:
    traffic = TRAFFIC if model == SMALL else COMPLEX_TRAFFIC
    return Scenario(
        name, title, (Fault(model, kind),), target=model, traffic=traffic, **more
    )


def _partial(name: str, title: str, kind: str, rate: float) -> Scenario:
    # 60 requests, 45 of them to the small model: more than two windows.
    return Scenario(
        name,
        title,
        (Fault(SMALL, kind, rate),),
        expect="measure",
        fault_requests=60,
    )


BOTH_DOWN = (Fault(SMALL, "outage"), Fault(LARGE, "outage"))
CATALOG: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("A1", "Healthy", target=None, expect="no_disable", fault_seconds=600),
        _one("A2", "Healthy after a disable", SMALL, "bad_format", after_requests=60),
        _one("B1", "Small model down", SMALL, "outage"),
        _one("B2", "Small model malformed", SMALL, "bad_format"),
        _one("B3", "Small model wrong intents", SMALL, "label_swap", semantic=True),
        _one("B4", "Large model down", LARGE, "outage"),
        _one("B5", "Large model malformed", LARGE, "bad_format"),
        _partial("C1", "Light", "bad_format", 0.1),
        _partial("C2", "Below threshold", "bad_format", 0.3),
        _partial("C3", "At threshold", "bad_format", 0.5),
        _partial("C4", "Above threshold", "bad_format", 0.7),
        _partial("C5", "Partial outage", "outage", 0.3),
        Scenario(
            "D1",
            "Both down",
            BOTH_DOWN,
            target=None,
            expect="kept_last",
            after_detect=10,
        ),  # fmt: skip
        Scenario(
            "D2",
            "Both malformed",
            (Fault(SMALL, "bad_format"), Fault(LARGE, "bad_format")),
            target=None,
            expect="kept_last",
            after_detect=10,
        ),
        Scenario(
            "D3",
            "One down, one malformed",
            (Fault(SMALL, "outage"), Fault(LARGE, "bad_format")),
            target=None,
            expect="kept_last",
            after_detect=10,
        ),
        Scenario(
            "D4",
            "Operator hold, then a fault",
            (Fault(SMALL, "outage"),),
            expect="kept_last",
            hold=LARGE,
            fault_requests=20,
        ),
        _one("E1", "Fault returns after recovery", SMALL, "bad_format", cycles=2),
        # 10 s into the 60 s cooldown, as in the doc.
        _one(
            "E2",
            "Fault cleared during the cooldown",
            SMALL,
            "outage",
            clear_after_cooldowns=1 / 6,
        ),  # fmt: skip
        Scenario(
            "E3",
            "Flapping",
            (Fault(SMALL, "bad_format"),),
            expect="measure",
            flap=(10, 10, 300),
        ),
        _one("E4", "Fault with no traffic", SMALL, "outage", quiet_seconds=120),
    ]
}
ALIASES = {"outage": "B1", "bad_format": "B2", "label_swap": "B3"}
SCENARIOS = tuple(ALIASES)  # the three built first, run when none is named
GROUPS = sorted({name[0] for name in CATALOG})


def scenario_for(name: str | Scenario) -> Scenario:
    if isinstance(name, Scenario):
        return name
    return CATALOG[ALIASES.get(name, name)]


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
    # normal, quiet, fault, off (E3) or cleared; after (A2); a second cycle
    # (E1) adds "#2", as in fault#2.
    phase: str
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
    def disabled(self) -> str | None:
        """The model this request's controller action disabled, if any."""
        action = self.controller_action or ""
        if not action.startswith("disabled "):
            return None
        return action.removeprefix("disabled ").split(":", 1)[0]

    @property
    def disabled_small(self) -> bool:
        return self.disabled == SMALL


@dataclass
class Result:
    scenario: str
    fault_at: float
    cleared_at: float | None = None
    title: str = ""
    expect: str = "disable"
    detected: bool = False
    # The model disabled first after the faults were set.
    disabled_model: str | None = None
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
    # Models the controller kept although they failed (kept_last_backend).
    kept_last: list[str] = field(default_factory=list)
    # The interventions logged during the run, oldest first.
    interventions: list[dict[str, Any]] = field(default_factory=list)
    # Measurements only some scenarios have: E1's second cycle, E3's flapping.
    extra: dict[str, Any] = field(default_factory=dict)
    # Why the run did not show what its scenario expects.
    problems: list[str] = field(default_factory=list)
    # Why the run stopped early, if it did; its other fields are then partial.
    error: str | None = None
    replies: list[Reply] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """The run showed what its scenario expects, with no error."""
        return not self.problems and self.error is None


def summarize(
    scenario: str,
    replies: list[Reply],
    fault_at: float,
    cleared_at: float | None = None,
    *,
    target: str | None = SMALL,
    faulted: set[str] | None = None,
    expect: str = "disable",
    logged: tuple[float, str] | None = None,
) -> Result:
    """Work out the measurements from the replies of one scenario. `fault_at`
    and `cleared_at` are when the fault was set and cleared, in seconds since
    the scenario started; each reply's `at` is when it arrived. `target` is
    the model whose disable counts as detection; None means any model in
    `faulted` (default: just the target).

    `logged` is the first disable in the intervention log, as (seconds since
    the start, model). It is used when no reply carries the disable: a request
    that disables a model can still end in a 503 when no model answers (D1),
    and a 503 has no trace."""
    faulted = {target} if faulted is None and target else (faulted or set())
    acting = {target} if target else faulted
    result = Result(
        scenario=scenario,
        fault_at=fault_at,
        cleared_at=cleared_at,
        expect=expect,
        replies=replies,
    )
    normal = [r for r in replies if r.phase == "normal"]
    fault = [r for r in replies if r.phase in ("quiet", "fault", "off")]
    cleared = [r for r in replies if r.phase == "cleared"]
    after = [r for r in replies if r.phase == "after"]
    # Any action with no fault on, and any disable of a model with no fault.
    result.false_interventions = sum(
        r.controller_action is not None for r in normal + after
    ) + sum(
        r.disabled is not None and r.disabled not in faulted for r in fault + cleared
    )
    result.unavailable = sum(r.status != 200 for r in fault + cleared)
    result.bad_replies = sum(not r.good for r in fault + cleared)
    detect = next((r for r in fault if r.disabled in acting), None)
    detect_at, disabled = (detect.at, detect.disabled) if detect else (None, None)
    during = logged is not None and (cleared_at is None or logged[0] < cleared_at)
    if detect is None and logged is not None and during and logged[1] in acting:
        # The request that logged it is the first to arrive after it.
        detect_at, disabled = logged
        detect = next((r for r in fault if r.at >= detect_at), None)
        if detect is None:
            detect_at = disabled = None
    before, rest = fault, []
    if detect is not None and detect_at is not None:
        result.detected = True
        result.disabled_model = disabled
        result.detect_seconds = round(detect_at - fault_at, 2)
        before = fault[: fault.index(detect) + 1]
        rest = fault[fault.index(detect) + 1 :]
        routes = {ROUTE[m] for m in acting}
        result.requests_to_detect = sum(r.route in routes for r in before)
    result.bad_replies_before_detect = sum(not r.good for r in before)
    if cleared_at is not None and result.detected:
        # From the moment the fault was cleared, not from the first reply
        # after it, which may itself have been slow.
        back = _back_at(cleared, faulted or acting)
        if back is not None:
            result.restored = True
            result.restore_seconds = round(back - cleared_at, 2)
    result.latency_ms_normal = _median(normal)
    result.latency_ms_fault = _median(before)
    result.latency_ms_after_disable = _median(rest)
    if result.false_interventions:
        result.problems.append("false intervention")
    # kept_last with no target (D1-D3): one model goes, the other is kept. With
    # a target (D4), that model is the last one and must not be disabled.
    needs_disable = expect == "disable" or (expect == "kept_last" and not target)
    if needs_disable:
        if not result.detected:
            result.problems.append("no model disabled")
        elif cleared_at is not None and not result.restored:
            result.problems.append("not serving again after the fault cleared")
    if expect == "no_disable" and result.detected:
        result.problems.append(f"{result.disabled_model} disabled")
    return result


def _back_at(cleared: list[Reply], models: set[str]) -> float | None:
    """When the last of `models` first answered its own route again."""
    times = []
    for model in models:
        back = next((r for r in cleared if _serves(r, model)), None)
        if back is None:
            return None
        times.append(back.at)
    return max(times) if times else None


def _serves(reply: Reply, model: str) -> bool:
    """`model` answered its own route with a good reply."""
    return reply.route == ROUTE[model] and reply.model == model and reply.good


def _small_serves(reply: Reply) -> bool:
    return _serves(reply, SMALL)


def _median(replies: list[Reply]) -> float | None:
    values = [r.latency_ms for r in replies if r.latency_ms is not None]
    return round(statistics.median(values), 1) if values else None


class Gateway:
    def __init__(self, send: Transport, token: str | None):
        self.send = send
        self.token = token
        self.sent = 0
        self.traffic: tuple[Message, ...] = TRAFFIC

    def _operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health(self) -> dict[str, Any]:
        status, body = self.send("GET", "/v1/health", None, {})
        if status != 200:
            raise RuntimeError(f"/v1/health answered {status}")
        return body

    def interventions(self) -> list[dict[str, Any]]:
        """The interventions /v1/health lists (the newest 50), oldest first."""
        items = self.health().get("interventions") or []
        return sorted(items, key=lambda i: i["at"])

    def fault(self, model: str, fault: str, rate: float = 1.0) -> None:
        payload: dict[str, Any] = {"model": model, "fault": fault}
        if fault != "none" and rate != 1.0:
            payload["rate"] = rate
        status, body = self.send("POST", "/v1/faults", payload, self._operator())
        if status != 200:
            raise RuntimeError(
                f"/v1/faults answered {status}: {body}. Set APP_OPERATOR_TOKEN, "
                "the same value, on the gateway and here."
            )

    def reset_windows(self) -> None:
        """Clear both models' monitor windows: an operator disable then enable
        of each, one at a time so one model always serves."""
        for model in (SMALL, LARGE):
            # Enable again even if the script is stopped right after the
            # disable, so no model is left on operator hold.
            try:
                self._operator_action(model, "disable")
            finally:
                self._operator_action(model, "enable")

    def hold(self, model: str) -> None:
        self._operator_action(model, "disable")

    def release(self, model: str) -> None:
        self._operator_action(model, "enable")

    def _operator_action(self, model: str, action: str) -> None:
        status, body = self.send(
            "POST",
            f"/v1/operator/models/{model}/{action}",
            {"reason": "measure_recovery.py: clean windows for a run"},
            self._operator(),
        )
        if status != 200:
            raise RuntimeError(
                f"/v1/operator/models/{model}/{action} answered {status}: {body}"
            )

    def chat(
        self,
        phase: str,
        start: float,
        clock: Callable[[], float],
        traffic: tuple[Message, ...] | None = None,
    ) -> Reply:
        """Send the next message of `traffic` (default: this run's traffic)."""
        messages = traffic or self.traffic
        message, route, intent = messages[self.sent % len(messages)]
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


@dataclass
class _Run:
    """What one run has sent and when, shared by its phases."""

    gateway: Gateway
    scenario: Scenario
    interval: float
    timeout: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    start: float = 0.0
    replies: list[Reply] = field(default_factory=list)
    marks: dict[str, float | None] = field(default_factory=dict)
    cooldown: float = 60.0
    # Interventions logged before the run, and the wall time it started, to
    # place the gateway's intervention times on the run's clock.
    seen: set[tuple[Any, ...]] = field(default_factory=set)
    wall_start: datetime | None = None

    def now(self) -> float:
        return round(self.clock() - self.start, 3)

    def new_interventions(self) -> list[dict[str, Any]]:
        return [i for i in self.gateway.interventions() if _key(i) not in self.seen]

    def first_logged_disable(
        self, models: set[str], items: list[dict[str, Any]] | None = None
    ) -> tuple[float, str] | None:
        """The first disable of one of `models` in the log, as (seconds since
        the start, model). Gateway time: compare on the same host only."""
        if self.wall_start is None:
            return None
        items = self.new_interventions() if items is None else items
        for i in items:
            if i["action"] == "disabled" and i["backend"] in models:
                at = (_time(i["at"]) - self.wall_start).total_seconds()
                return round(at, 3), i["backend"]
        return None

    def tick(self, phase: str, traffic: tuple[Message, ...] | None = None) -> Reply:
        sent = self.clock()
        reply = self.gateway.chat(phase, self.start, self.clock, traffic)
        self.replies.append(reply)
        # A message every `interval` seconds, not a pause after each reply.
        self.sleep(max(0.0, self.interval - (self.clock() - sent)))
        return reply

    def set_faults(self) -> None:
        for f in self.scenario.faults:
            self.gateway.fault(f.model, f.kind, f.rate)

    def clear_faults(self) -> None:
        for model in (SMALL, LARGE):
            self.gateway.fault(model, "none")


def run_scenario(
    gateway: Gateway,
    scenario: str | Scenario,
    *,
    interval: float,
    normal_requests: int = 8,
    after_detect: int | None = None,
    timeout: float = 180,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Run one scenario and return its measurements. Clears every fault and
    both monitor windows before it starts, and the faults (and any hold) after
    it ends. An error partway returns what was measured so far, with `error`
    set."""
    spec = scenario_for(scenario)
    if after_detect is not None:
        spec = replace(spec, after_detect=after_detect)
    run = _Run(gateway, spec, interval, timeout, clock, sleep, start=clock())
    run.marks = {"fault_at": None, "cleared_at": None}
    error: str | None = None
    try:
        # Setup is inside the try: a failure here is reported as a failed run
        # and does not stop the runs after it.
        for model in (SMALL, LARGE):
            gateway.fault(model, "none")
        health = _wait_until_serving(gateway, interval, timeout, clock, sleep)
        run.cooldown = float(
            (health.get("controller") or {}).get("cooldown_seconds", 60)
        )
        gateway.reset_windows()
        run.seen = {_key(i) for i in gateway.interventions()}
        gateway.sent = 0
        gateway.traffic = spec.traffic
        run.start = clock()
        # timezone.utc, not datetime.UTC: the script runs on Python 3.9.
        run.wall_start = datetime.now(timezone.utc)  # noqa: UP017
        for _ in range(normal_requests):
            run.tick("normal")
        if spec.faults or spec.hold:
            _fault_cycle(run, "")
            for cycle in range(2, spec.cycles + 1):
                _fault_cycle(run, f"#{cycle}")
        elif spec.fault_seconds:
            # A1: no fault at all, so the whole run is normal traffic.
            _for_seconds(run, "normal", spec.fault_seconds)
        for _ in range(spec.after_requests):
            run.tick("after")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        gateway.traffic = TRAFFIC
    interventions: list[dict[str, Any]] = []
    problem: str | None = None
    try:
        interventions = run.new_interventions()
    except Exception as exc:  # the run's own error says more
        problem = f"{type(exc).__name__}: {exc}"
    fault_at = run.marks["fault_at"]
    acting = {spec.target} if spec.target else spec.faulted
    result = summarize(
        spec.name,
        run.replies,
        fault_at if fault_at is not None else 0.0,
        run.marks["cleared_at"],
        target=spec.target,
        faulted=spec.faulted,
        expect=spec.expect,
        logged=run.first_logged_disable(acting, interventions),
    )
    result.title = spec.title
    result.interventions = interventions
    if problem:
        result.extra["interventions_error"] = problem
    if fault_at is None:
        # The fault was never set: there is no detection time to report.
        result.detect_seconds = None
    result.error = error
    _check_scenario(result, spec, run)
    return result


def _fault_cycle(run: _Run, cycle: str) -> None:
    """Set the faults, send traffic through them, clear them, and send until
    every faulted model serves again. `cycle` is "" or "#2"."""
    spec, gateway = run.scenario, run.gateway
    fault_at = run.now()
    try:
        if spec.hold:
            gateway.hold(spec.hold)
        run.set_faults()
        # Recorded as soon as the fault is set, so a run that fails later
        # still reports the right detection time.
        run.marks[f"fault_at{cycle}"] = fault_at
        if spec.quiet_seconds:
            # E4: no traffic reaches the faulted model's route for a while.
            quiet = (
                (COMPLEX,) if ROUTE[spec.target or SMALL] == "standard" else (STANDARD,)
            )
            _for_seconds(run, "quiet" + cycle, spec.quiet_seconds, quiet)
            run.marks["quiet_end"] = run.now()
        if spec.flap:
            _flap(run)
        elif spec.fault_requests:
            for _ in range(spec.fault_requests):
                run.tick("fault" + cycle)
        else:
            acting = {spec.target} if spec.target else spec.faulted
            while run.now() - fault_at < run.timeout:
                reply = run.tick("fault" + cycle)
                if reply.disabled in acting:
                    break
                # A 503 has no trace, so look in the log (D1).
                if reply.status != 200 and run.first_logged_disable(acting):
                    break
            for _ in range(spec.after_detect):
                run.tick("fault" + cycle)
            if spec.clear_after_cooldowns:
                _for_seconds(
                    run, "fault" + cycle, spec.clear_after_cooldowns * run.cooldown
                )
    finally:
        run.clear_faults()
        if spec.hold:
            gateway.release(spec.hold)
    cleared_at = run.now()
    run.marks[f"cleared_at{cycle}"] = cleared_at
    models = spec.faulted or {SMALL}
    back: set[str] = set()
    while run.now() - cleared_at < run.timeout and back != models:
        reply = run.tick("cleared" + cycle)
        back |= {m for m in models if _serves(reply, m)}


def _flap(run: _Run) -> None:
    """E3: the faults on for `on` seconds, off for `off`, for `total`."""
    on, off, total = run.scenario.flap or (0, 0, 0)
    began = run.now()
    while run.now() - began < total:
        _for_seconds(run, "fault", on)
        run.clear_faults()
        _for_seconds(run, "off", off)
        if run.now() - began < total:
            run.set_faults()


def _for_seconds(
    run: _Run,
    phase: str,
    seconds: float,
    traffic: tuple[Message, ...] | None = None,
) -> None:
    began = run.now()
    while run.now() - began < seconds:
        run.tick(phase, traffic)


def _check_scenario(result: Result, spec: Scenario, run: _Run) -> None:
    """The checks that need more than one fault phase or the intervention log."""
    cleared_at = result.cleared_at
    if cleared_at is not None and run.wall_start is not None:
        # A model disabled after its fault was cleared, on failures still in
        # its window: after D4's hold is released, for one.
        late = []
        for i in result.interventions:
            if i["action"] == "disabled":
                at = (_time(i["at"]) - run.wall_start).total_seconds()
                if at > cleared_at:
                    late.append({"model": i["backend"], "seconds_after_clear":
                                 round(at - cleared_at, 2)})  # fmt: skip
        if late:
            result.extra["disabled_after_clear"] = late
    logged = result.interventions
    result.kept_last = sorted(
        {i["backend"] for i in logged if i["action"] == "kept_last_backend"}
    )
    if spec.expect == "kept_last" and not result.kept_last:
        result.problems.append("no kept_last_backend logged")
    if spec.hold and result.detected:
        result.problems.append(f"{result.disabled_model} disabled under a hold")
    if spec.cycles > 1:
        later = [r for r in run.replies if "#" in r.phase]
        second = summarize(
            spec.name,
            [replace(r, phase=r.phase.split("#")[0]) for r in later],
            run.marks.get("fault_at#2") or 0.0,
            run.marks.get("cleared_at#2"),
            target=spec.target,
            faulted=spec.faulted,
        )
        result.extra["second_cycle"] = {
            "detected": second.detected,
            "requests_to_detect": second.requests_to_detect,
            "detect_seconds": second.detect_seconds,
            "restore_seconds": second.restore_seconds,
        }
        result.problems += [f"second cycle: {p}" for p in second.problems]
    if spec.quiet_seconds:
        quiet_end = run.marks.get("quiet_end")
        early = [
            r for r in run.replies if r.phase == "quiet" and r.disabled in spec.faulted
        ]
        result.extra["quiet_seconds"] = spec.quiet_seconds
        if early:
            result.problems.append("disabled while no traffic reached it")
        if quiet_end is not None and result.detect_seconds is not None:
            fault_at = result.fault_at
            result.extra["detect_seconds_after_quiet"] = round(
                result.detect_seconds - (quiet_end - fault_at), 2
            )
    if spec.flap:
        result.extra |= _flap_measures(result.interventions, spec)


def _flap_measures(
    interventions: list[dict[str, Any]], spec: Scenario
) -> dict[str, Any]:
    """E3: how often each faulted model was disabled, and for how long."""
    total = (spec.flap or (0, 0, 0))[2]
    out: dict[str, Any] = {}
    for model in sorted(spec.faulted):
        events = [i for i in interventions if i["backend"] == model]
        disables = [i for i in events if i["action"] == "disabled"]
        seconds = 0.0
        for d in disables:
            back = next(
                (
                    i
                    for i in events
                    if i["action"] == "re-enabled" and i["at"] > d["at"]
                ),
                None,
            )
            if back is not None:
                seconds += (_time(back["at"]) - _time(d["at"])).total_seconds()
        out[model] = {
            "disables": len(disables),
            "seconds_out": round(seconds, 1),
            "share_out": round(seconds / total, 3) if total else None,
        }
    return {"flapping": out}


def _time(value: str) -> datetime:
    """Parse the gateway's ISO time. Before Python 3.11, fromisoformat needs a
    +00:00 offset and exactly 6 fraction digits, and the gateway trims them."""
    text = value.replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    return datetime.fromisoformat(text)


def _key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get("at"), item.get("backend"), item.get("action"), item.get("reason"))


def _wait_until_serving(
    gateway: Gateway,
    interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Wait out a cooldown or hold left from an earlier run. A re-enable
    happens on the next check, and /v1/health is a check."""
    give_up = clock() + timeout
    while True:
        health = gateway.health()
        if health["status"] == "ok":
            return health
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
        "scenario  disabled    detect_s  requests  bad_before  bad_all  503s  "
        "false  restore_s  p50_ms normal/fault/after"
    )
    print(header)
    for r in results:
        notes = list(r.problems)
        if r.kept_last:
            notes.append(f"kept last: {', '.join(r.kept_last)}")
        for late in r.extra.get("disabled_after_clear", []):
            notes.append(
                f"{late['model']} disabled {late['seconds_after_clear']} s after "
                "the clear"
            )
        if "flapping" in r.extra:
            notes += [
                f"{m}: {v['disables']} disables, {v['share_out']:.0%} out"
                for m, v in r.extra["flapping"].items()
                if v["share_out"] is not None
            ]
        print(
            f"{r.scenario:<9} {_cell(r.disabled_model):<10}  "
            f"{_cell(r.detect_seconds):>8}  "
            f"{_cell(r.requests_to_detect):>8}  {r.bad_replies_before_detect:>10}  "
            f"{r.bad_replies:>7}  {r.unavailable:>4}  {r.false_interventions:>5}  "
            f"{_cell(r.restore_seconds):>9}  "
            f"{_cell(r.latency_ms_normal)}/{_cell(r.latency_ms_fault)}/"
            f"{_cell(r.latency_ms_after_disable)}"
            + (f"  [{'; '.join(notes)}]" if notes else "")
            + (f"  FAILED: {r.error}" if r.error else "")
        )
    by_name: dict[str, list[Result]] = {}
    for r in results:
        by_name.setdefault(r.scenario, []).append(r)
    for name, runs in by_name.items():
        if len(runs) > 1:
            done = [r for r in runs if r.error is None]
            hits = sum(r.detected for r in done)
            asked = [r.requests_to_detect for r in done if r.requests_to_detect]
            median = statistics.median(asked) if asked else "-"
            print(
                f"{name}: a model disabled in {hits} of {len(done)} completed runs; "
                f"median requests to detect {median}"
            )


def _cell(value: float | int | str | None) -> str:
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


def chosen(scenarios: list[str] | None, groups: list[str] | None) -> list[str]:
    """Scenario names in catalog order: those named, those in the groups, or
    B1-B3 when neither is given."""
    if not scenarios and not groups:
        return [ALIASES[name] for name in SCENARIOS]
    names = {ALIASES.get(s, s) for s in scenarios or []}
    names |= {n for n in CATALOG if n[0] in (groups or [])}
    return [n for n in CATALOG if n in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--scenario", choices=[*CATALOG, *ALIASES], action="append",
        help="a scenario to run (repeatable)",
    )  # fmt: skip
    parser.add_argument(
        "--group", choices=GROUPS, action="append", help="every scenario in a group"
    )
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
        for name in chosen(args.scenario, args.group):
            spec = CATALOG[name]
            if spec.semantic and not label_swap_ready(health):
                print(
                    f"Skipping {name} (label_swap): it needs "
                    "APP_SEMANTIC_ACTIONS_ENABLED=true and a baseline "
                    "(compose.measure.yml sets both)."
                )
                continue
            for run in range(args.runs):
                print(
                    f"Running {name}, {spec.title} ({run + 1} of {args.runs})...",
                    flush=True,
                )
                result = run_scenario(gateway, spec, interval=args.interval)
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
