"""Circuit breaker for the Jev call.

After `failures` whole-call failures in a row, Jev is skipped for
`cooldown_seconds` and the rule router routes every message. After the
cool-off, one message tries Jev again. If it succeeds the breaker closes; if it
fails the breaker opens for another cool-off. Messages that arrive while that
trial call is running are also skipped, so an outage costs one 2-second wait
per cool-off, not one per message.

An invalid answer to one question is not a whole-call failure: Jev answered.
"""

from collections.abc import Callable
from typing import Literal

BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self, failures: int, cooldown_seconds: float, clock: Callable[[], float]
    ):
        self.failure_limit = failures
        self.cooldown = cooldown_seconds
        self.clock = clock
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self.trial_running = False

    def state(self) -> BreakerState:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at < self.cooldown:
            return "open"
        return "half_open"

    def allow(self) -> bool:
        """True if this message may call Jev. Call exactly one of `succeeded`,
        `failed` or `abandoned` after an allowed call."""
        state = self.state()
        if state == "closed":
            return True
        if state == "open" or self.trial_running:
            return False
        self.trial_running = True
        return True

    def succeeded(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.trial_running = False

    def failed(self) -> None:
        # The count only resets on success, so a failed trial is always at or
        # past the limit and reopens the breaker.
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_limit:
            self.opened_at = self.clock()
        self.trial_running = False

    def abandoned(self) -> None:
        """The request was cancelled mid-call; Jev neither passed nor failed."""
        self.trial_running = False
