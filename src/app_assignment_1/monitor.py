"""In-memory failure monitor and controller for the backends.

Windows and disabled backends live in the gateway process and reset on restart.
The gateway saves each intervention to its store (see store.py), which may be
Postgres; `unsaved` holds the ones it has not saved yet.
"""

import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

from app_assignment_1.config import Settings
from app_assignment_1.schemas import (
    BackendMonitor,
    BackendName,
    ControllerRules,
    Intervention,
    InterventionAction,
)

Outcome = Literal["ok", "format_failure", "call_error"]
DisabledFor = Literal["failure", "semantic"]


class Controller:
    """Disables a backend whose recent failure rate crosses a threshold.

    A failure is a reply that fails the format check or a call that raises.

    Rules: a sliding window per backend, a minimum sample count before acting,
    a cooldown after which the backend is re-enabled with a fresh window, and an
    action limit that never disables the last usable backend.

    A model whose release was just deployed is watched (see releases.py). While
    it is, crossing the threshold rolls the release back instead: the model
    keeps serving on its rollback target, with a fresh window, so a rollback
    also works on the last usable model.
    """

    def __init__(
        self,
        names: Iterable[BackendName],
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.windows: dict[BackendName, deque[Outcome]] = {
            name: deque(maxlen=settings.monitor_window) for name in names
        }
        self.window = settings.monitor_window
        self.min_samples = settings.monitor_min_samples
        self.threshold = settings.monitor_failure_threshold
        self.cooldown = settings.controller_cooldown_seconds
        self.clock = clock
        self.disabled_until: dict[BackendName, float] = {}
        # Why each disabled model is out. One disabled only for wrong intents
        # still answers as a last resort (see `last_resort`).
        self.disabled_for: dict[BackendName, DisabledFor] = {}
        # Disabled by an operator: no cooldown, only an operator enables it.
        self.held: set[BackendName] = set()
        self.warned: set[BackendName] = set()
        self.log: deque[Intervention] = deque(maxlen=50)
        # Every intervention since start; the log above keeps only the latest.
        self.action_counts: Counter[tuple[str, BackendName]] = Counter()
        self.unsaved: list[Intervention] = []
        # Called with a model's name when its cooldown ends, so other monitors
        # can start that model's window again too.
        self.on_reenable: list[Callable[[BackendName], None]] = []
        # Models above the semantic baseline that could not be disabled, so
        # kept_last_backend is logged once per run, not on every reply.
        self.semantic_warned: set[BackendName] = set()
        # Models with a release under watch: calls counted since the deploy.
        # Never shorter than the evidence the checks need before they act:
        # the failure minimum, and the semantic minimum when that acts too.
        self.watch_samples = max(
            settings.release_watch_samples,
            settings.monitor_min_samples,
            settings.semantic_min_samples if settings.semantic_actions_enabled else 0,
        )
        self.watching: dict[BackendName, int] = {}
        # Roll a model back; return the version it serves now, or None if it
        # has nowhere to go. Set by the gateway.
        self.roll_back: Callable[[BackendName, str], str | None] | None = None
        # Called with a model's name and its count while watching, and when
        # the watch ends with the release confirmed.
        self.on_watched: list[Callable[[BackendName, int], None]] = []
        self.on_confirm: list[Callable[[BackendName], None]] = []

    def _note(
        self,
        name: BackendName,
        action: InterventionAction,
        reason: str,
        ago: float = 0,
    ) -> None:
        at = datetime.now(UTC) - timedelta(seconds=ago)
        item = Intervention(at=at, backend=name, action=action, reason=reason)
        self.log.append(item)
        self.unsaved.append(item)
        self.action_counts[(action, name)] += 1

    def operator_disable(self, name: BackendName, reason: str) -> None:
        """Hold a backend disabled until operator_enable. The caller checks that
        another backend stays usable."""
        if name in self.held:
            return
        self.held.add(name)
        self._note(name, "operator_disabled", reason)

    def operator_enable(self, name: BackendName, reason: str) -> None:
        """Lift an operator hold and any controller cooldown, with a fresh window."""
        if name not in self.held and name not in self.disabled_until:
            return
        self.held.discard(name)
        self.disabled_until.pop(name, None)
        self.disabled_for.pop(name, None)
        self.windows[name].clear()
        self.warned.discard(name)
        self.semantic_warned.discard(name)
        # Like a re-enable at the end of a cooldown: the semantic window
        # starts again too.
        for reset in self.on_reenable:
            reset(name)
        self._note(name, "operator_enabled", reason)

    def watch(self, name: BackendName) -> None:
        """Start watching a new release, with a fresh window: calls to the
        version before it say nothing about this one."""
        self.watching[name] = 0
        self.windows[name].clear()
        self.warned.discard(name)
        self.semantic_warned.discard(name)

    def unwatch(self, name: BackendName) -> None:
        """End a watch after a rollback, with a fresh window for the version
        the model serves now."""
        self.watching.pop(name, None)
        self.windows[name].clear()
        self.warned.discard(name)
        self.semantic_warned.discard(name)

    def _count_watch(self, name: BackendName) -> None:
        if name not in self.watching:
            return
        if self.watching[name] >= self.watch_samples:
            return
        self.watching[name] += 1
        count = self.watching[name]
        for hook in self.on_watched:
            hook(name, count)

    def finish_call(self, name: BackendName) -> None:
        """After every check on one call, the semantic one included: confirm
        a watched release that has served its watch below the threshold.
        Confirming here, not in `record`, gives the semantic check on the
        last call of the watch its chance to roll back."""
        count = self.watching.get(name)
        if count is None or count < self.watch_samples or self._failing(name):
            return
        del self.watching[name]
        for confirm in self.on_confirm:
            confirm(name)

    def _roll_back(self, name: BackendName, reason: str) -> str | None:
        """Roll back a watched release. None if there is nowhere to go, and
        the usual disable rules apply."""
        if self.roll_back is None:
            return None
        version = self.roll_back(name, reason)
        if version is None:
            return None
        # The gateway's release hook has already called unwatch.
        self.watching.pop(name, None)
        self._note(name, "rolled_back", f"back to {version}; {reason}")
        return f"rolled back {name} to {version}: {reason}"

    def is_disabled(self, name: BackendName) -> bool:
        # Check the cooldown first, so one that ended under a hold still logs.
        cooling = self._cooling_down(name)
        return cooling or name in self.held

    def _cooling_down(self, name: BackendName) -> bool:
        until = self.disabled_until.get(name)
        if until is None:
            return False
        now = self.clock()
        if now < until:
            return True
        # Re-enabling happens on the next check; log when the cooldown ended.
        del self.disabled_until[name]
        self.disabled_for.pop(name, None)
        self.windows[name].clear()
        for reset in self.on_reenable:
            reset(name)
        self._note(name, "re-enabled", "cooldown elapsed; window reset", now - until)
        return False

    def record(
        self, name: BackendName, failed: bool, can_disable: bool, own_route: bool = True
    ) -> str | None:
        """Record one reply's format check; return the action taken, if any.
        Only calls on the model's own route count toward a release's watch:
        the semantic check sees only those, so rerouted calls alone must not
        confirm a release."""
        outcome: Outcome = "format_failure" if failed else "ok"
        return self._record(name, outcome, can_disable, own_route)

    def record_error(
        self, name: BackendName, can_disable: bool, own_route: bool = True
    ) -> str | None:
        """Record one call that raised; return the action taken, if any."""
        return self._record(name, "call_error", can_disable, own_route)

    def _record(
        self, name: BackendName, outcome: Outcome, can_disable: bool, own_route: bool
    ) -> str | None:
        window = self.windows[name]
        window.append(outcome)
        failures = sum(item != "ok" for item in window)
        rate = failures / len(window)
        if len(window) < self.min_samples or rate < self.threshold:
            self.warned.discard(name)
            if own_route:
                self._count_watch(name)
            return None
        if name in self.disabled_until and name not in self.watching:
            return None
        errors = window.count("call_error")
        kind = (
            "format failure"
            if not errors
            else "call error"
            if errors == failures
            else "failure"
        )
        reason = f"{kind} rate {rate:.0%} over last {len(window)} calls"
        if name in self.watching:
            action = self._roll_back(name, reason)
            if action is not None:
                return action
            self.watching.pop(name, None)
            if name in self.disabled_until:
                return None
        return self._disable(name, reason, can_disable, self.warned, "failure")

    def _failing(self, name: BackendName) -> bool:
        window = self.windows[name]
        failures = sum(item != "ok" for item in window)
        return bool(window) and failures / len(window) >= self.threshold

    def answering(self, name: BackendName) -> bool:
        """The model has calls in its window and its failure rate is below the
        threshold. An empty window, as after a re-enable, is not evidence."""
        return bool(self.windows[name]) and not self._failing(name)

    def last_resort(self, name: BackendName) -> bool:
        """The model is disabled only because its intents differ from Jev's.
        The gateway still calls it when no other model answers: a reply with a
        possibly wrong intent is better than a 503. Its own failure rate does
        not matter, as for an enabled model that is the last one left: a model
        that answers some calls beats a certain 503, and excluding it would
        freeze its window until the cooldown ends. A model an operator holds
        is never a last resort."""
        return (
            name not in self.held
            and self.is_disabled(name)
            and self.disabled_for.get(name) == "semantic"
        )

    def record_semantic(
        self, name: BackendName, reason: str | None, can_disable: bool
    ) -> str | None:
        """Act on the semantic monitor: `reason` is set while the model is
        above the baseline, None otherwise. Return the action taken, if any."""
        if reason is None:
            self.semantic_warned.discard(name)
            return None
        if name in self.watching:
            action = self._roll_back(name, reason)
            if action is not None:
                return action
            self.watching.pop(name, None)
        if name in self.disabled_until:
            return None
        return self._disable(
            name, reason, can_disable, self.semantic_warned, "semantic"
        )

    def _disable(
        self,
        name: BackendName,
        reason: str,
        can_disable: bool,
        warned: set[BackendName],
        kind: DisabledFor,
    ) -> str | None:
        if not can_disable:
            # Log once per run above the threshold, not on every failing request.
            if name not in warned:
                warned.add(name)
                self._note(name, "kept_last_backend", reason)
            return None
        warned.discard(name)
        self.disabled_until[name] = self.clock() + self.cooldown
        self.disabled_for[name] = kind
        self._note(name, "disabled", reason)
        return f"disabled {name}: {reason}"

    def rules(self) -> ControllerRules:
        return ControllerRules(
            window=self.window,
            min_samples=self.min_samples,
            failure_threshold=self.threshold,
            cooldown_seconds=self.cooldown,
        )

    def stats(self) -> list[BackendMonitor]:
        result = []
        for name, window in self.windows.items():
            disabled = self._cooling_down(name)
            format_failures = window.count("format_failure")
            call_errors = window.count("call_error")
            failures = format_failures + call_errors
            result.append(
                BackendMonitor(
                    name=name,
                    samples=len(window),
                    format_failures=format_failures,
                    call_errors=call_errors,
                    failure_rate=round(failures / len(window), 2) if window else None,
                    disabled_by_controller=disabled,
                    disabled_for=self.disabled_for.get(name) if disabled else None,
                    answering=self.answering(name),
                    disabled_by_operator=name in self.held,
                    disabled_for_seconds=(
                        round(self.disabled_until[name] - self.clock(), 1)
                        if disabled
                        else None
                    ),
                )
            )
        return result
