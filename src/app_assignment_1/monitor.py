"""In-memory format-failure monitor and controller for the backends.

State lives in the gateway process and resets on restart.
"""

import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

from app_assignment_1.config import Settings
from app_assignment_1.schemas import (
    BackendMonitor,
    BackendName,
    ControllerRules,
    Intervention,
)


class Controller:
    """Disables a backend whose recent format-failure rate crosses a threshold.

    Rules: a sliding window per backend, a minimum sample count before acting,
    a cooldown after which the backend is re-enabled with a fresh window, and an
    action limit that never disables the last usable backend.
    """

    def __init__(
        self,
        names: Iterable[BackendName],
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.windows: dict[BackendName, deque[bool]] = {
            name: deque(maxlen=settings.monitor_window) for name in names
        }
        self.window = settings.monitor_window
        self.min_samples = settings.monitor_min_samples
        self.threshold = settings.monitor_failure_threshold
        self.cooldown = settings.controller_cooldown_seconds
        self.clock = clock
        self.disabled_until: dict[BackendName, float] = {}
        self.warned: set[BackendName] = set()
        self.log: deque[Intervention] = deque(maxlen=50)

    def _note(
        self,
        name: BackendName,
        action: Literal["disabled", "re-enabled", "kept_last_backend"],
        reason: str,
        ago: float = 0,
    ) -> None:
        at = datetime.now(UTC) - timedelta(seconds=ago)
        self.log.append(Intervention(at=at, backend=name, action=action, reason=reason))

    def is_disabled(self, name: BackendName) -> bool:
        until = self.disabled_until.get(name)
        if until is None:
            return False
        now = self.clock()
        if now < until:
            return True
        # Re-enabling happens on the next check; log when the cooldown ended.
        del self.disabled_until[name]
        self.windows[name].clear()
        self._note(name, "re-enabled", "cooldown elapsed; window reset", now - until)
        return False

    def record(self, name: BackendName, failed: bool, can_disable: bool) -> str | None:
        """Record one response; return the action taken, if any."""
        window = self.windows[name]
        window.append(failed)
        rate = sum(window) / len(window)
        if len(window) < self.min_samples or rate < self.threshold:
            self.warned.discard(name)
            return None
        if name in self.disabled_until:
            return None
        reason = f"format failure rate {rate:.0%} over last {len(window)} responses"
        if not can_disable:
            # Log once per run above the threshold, not on every failing request.
            if name not in self.warned:
                self.warned.add(name)
                self._note(name, "kept_last_backend", reason)
            return None
        self.warned.discard(name)
        self.disabled_until[name] = self.clock() + self.cooldown
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
            disabled = self.is_disabled(name)
            failures = sum(window)
            result.append(
                BackendMonitor(
                    name=name,
                    samples=len(window),
                    format_failures=failures,
                    failure_rate=round(failures / len(window), 2) if window else None,
                    disabled_by_controller=disabled,
                    disabled_for_seconds=(
                        round(self.disabled_until[name] - self.clock(), 1)
                        if disabled
                        else None
                    ),
                )
            )
        return result
