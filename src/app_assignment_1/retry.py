"""Retries with exponential backoff and full jitter.

Why a repeat is safe: a call to a model server only reads. At temperature 0
it holds no state and has no side effect, and the gateway saves the request
once, after one reply is chosen. So a repeated call can at worst cost a little
compute; it cannot double a charge, send a second message or store a second
request. Only failures that say "try again" are repeated (see
`vllm.is_transient`); a bad request or a malformed reply is not.

Retries spend the same time budget as the first try. The caller wraps the
whole loop in its attempt deadline (`Gateway._attempt`), so a retry can never
make a request outlast `APP_REQUEST_DEADLINE_SECONDS`, and each wait is also
capped by `max_wait`.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def backoff_delay(
    attempt: int,
    base: float,
    max_wait: float,
    retry_after: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float | None:
    """Seconds to wait after failed try number `attempt` (1 is the first).

    Full jitter: a random value up to `base * 2 ** (attempt - 1)`, capped at
    `max_wait`. A server's Retry-After is a floor on the wait; one longer than
    `max_wait` returns None, meaning give up now rather than hold the customer.
    """
    if retry_after is not None and retry_after > max_wait:
        return None
    ceiling = min(max_wait, base * 2 ** (attempt - 1))
    jittered = rng() * ceiling
    return max(jittered, retry_after or 0.0)


async def retry_async(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base: float,
    max_wait: float,
    is_transient: Callable[[Exception], bool],
    retry_after: Callable[[Exception], float | None] = lambda exc: None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Run `call`, repeating it after a transient failure, up to `attempts`
    tries in all. The last failure is raised unchanged."""
    attempt = 1
    while True:
        try:
            return await call()
        except Exception as exc:
            if attempt >= attempts or not is_transient(exc):
                raise
            delay = backoff_delay(attempt, base, max_wait, retry_after(exc), rng)
            if delay is None:
                raise
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await sleep(delay)
            attempt += 1
