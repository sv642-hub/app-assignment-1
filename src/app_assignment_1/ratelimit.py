"""A per-client token bucket for the model routes.

Each client gets `burst` requests at once, refilled at `per_minute / 60` a
second. A request over the limit is answered 429 with a Retry-After header
that says, in whole seconds, when a token will next be free. The state lives
in this process, which is enough for the one gateway container; a second
gateway would need a shared store.
"""

import math
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic

# Clients remembered at once. Past this, the least recently seen are dropped:
# a dropped client simply starts with a full bucket, so the cap only bounds
# memory and never blocks anyone.
MAX_CLIENTS = 10_000


class RateLimiter:
    """Token buckets keyed by client. `per_minute` of 0 turns the limit off."""

    def __init__(
        self,
        per_minute: float,
        burst: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Set the refill rate, the bucket size and the clock."""
        self.rate = per_minute / 60
        self.burst = burst
        self.clock = clock
        # client -> (tokens left, time of last use)
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        """Whether any limit applies."""
        return self.rate > 0

    def check(self, client: str) -> int | None:
        """Take one token for `client`. Return None if allowed, else the whole
        seconds to wait before a token is free."""
        if not self.enabled:
            return None
        now = self.clock()
        tokens, last = self.buckets.pop(client, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate)
        if tokens >= 1:
            allowed = None
            tokens -= 1
        else:
            allowed = max(1, math.ceil((1 - tokens) / self.rate))
        self.buckets[client] = (tokens, now)
        while len(self.buckets) > MAX_CLIENTS:
            self.buckets.popitem(last=False)
        return allowed
