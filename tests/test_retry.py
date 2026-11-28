"""Retries: backoff and jitter, which failures repeat, and the time budget."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app_assignment_1.labels import load_labels
from app_assignment_1.retry import backoff_delay, retry_async
from app_assignment_1.schemas import BackendConfig
from app_assignment_1.vllm import (
    BackendCallError,
    BackendTimeoutError,
    VLLMBackend,
    build_system_prompt,
    parse_retry_after,
)

LABELS = load_labels(Path("config/labels.json"))
GOOD = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {"intent": "card_arrival", "reply": "ok", "policy_facts": []}
                )
            }
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 9},
}


def test_backoff_grows_and_is_capped_with_full_jitter() -> None:
    top = lambda: 0.999999  # noqa: E731 - the largest jitter
    assert backoff_delay(1, 0.25, 2.0, rng=top) == pytest.approx(0.25, abs=1e-3)
    assert backoff_delay(2, 0.25, 2.0, rng=top) == pytest.approx(0.5, abs=1e-3)
    assert backoff_delay(3, 0.25, 2.0, rng=top) == pytest.approx(1.0, abs=1e-3)
    assert backoff_delay(9, 0.25, 2.0, rng=top) == pytest.approx(2.0, abs=1e-3)
    assert backoff_delay(3, 0.25, 2.0, rng=lambda: 0.0) == 0.0


def test_jitter_spreads_the_waits() -> None:
    waits = {backoff_delay(3, 0.25, 2.0) for _ in range(50)}
    assert len(waits) > 40
    assert all(0 <= w <= 1.0 for w in waits)


def test_retry_after_is_a_floor_and_a_too_long_one_gives_up() -> None:
    assert backoff_delay(1, 0.25, 2.0, retry_after=1.5, rng=lambda: 0.0) == 1.5
    assert backoff_delay(1, 0.25, 2.0, retry_after=30) is None


def test_parse_retry_after() -> None:
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after(" 0.5 ") == 0.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-4") is None
    assert parse_retry_after("inf") is None


def test_retry_async_repeats_only_transient_failures() -> None:
    calls: list[int] = []
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("down")
        return "ok"

    async def run(call) -> str:
        return await retry_async(
            call,
            attempts=4,
            base=0.1,
            max_wait=1,
            is_transient=lambda exc: isinstance(exc, ConnectionError),
            sleep=sleep,
            rng=lambda: 1.0,
        )

    assert asyncio.run(run(flaky)) == "ok"
    assert len(calls) == 3
    assert waited == [0.1, 0.2]

    async def bad() -> str:
        raise ValueError("no")

    with pytest.raises(ValueError):
        asyncio.run(run(bad))


def test_retry_async_gives_up_after_the_last_try() -> None:
    calls: list[int] = []

    async def down() -> str:
        calls.append(1)
        raise ConnectionError("down")

    async def sleep(seconds: float) -> None:
        pass

    with pytest.raises(ConnectionError):
        asyncio.run(
            retry_async(
                down,
                attempts=3,
                base=0.1,
                max_wait=1,
                is_transient=lambda exc: True,
                sleep=sleep,
            )
        )
    assert len(calls) == 3


class Server:
    """A vLLM stand-in that answers from a script and counts calls."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.script.pop(0) if self.script else self.script_last
        self.script_last = step
        if isinstance(step, Exception):
            raise step
        return step


def run_backend(server: Server, attempts: int = 3, max_seconds: float = 2.0):
    """Call a VLLMBackend against `server`. Returns (result or error, waits)."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def go():
        config = BackendConfig(name="qwen3-1.7b", route="standard", latency_ms=0)
        async with httpx.AsyncClient(
            base_url="http://vllm.test", transport=httpx.MockTransport(server)
        ) as client:
            backend = VLLMBackend(
                config,
                client,
                "m",
                build_system_prompt(LABELS, {}),
                LABELS,
                retry_attempts=attempts,
                retry_base_seconds=0.25,
                retry_max_seconds=max_seconds,
                sleep=sleep,
            )
            try:
                return await backend.generate("When will my card arrive?")
            except Exception as exc:
                return exc

    return asyncio.run(go()), waits


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_brief_server_error_is_retried_then_succeeds(status) -> None:
    server = Server(httpx.Response(status), httpx.Response(200, json=GOOD))
    result, waits = run_backend(server)
    assert result.intent == "card_arrival"
    assert server.calls == 2
    assert len(waits) == 1 and 0 <= waits[0] <= 0.25
    assert (result.prompt_tokens, result.completion_tokens) == (50, 9)


def test_a_refused_connection_is_retried() -> None:
    server = Server(httpx.ConnectError("refused"), httpx.Response(200, json=GOOD))
    result, _ = run_backend(server)
    assert result.intent == "card_arrival"
    assert server.calls == 2


def test_a_timeout_is_retried() -> None:
    server = Server(httpx.ReadTimeout("slow"), httpx.Response(200, json=GOOD))
    result, _ = run_backend(server)
    assert result.intent == "card_arrival"


def test_429_waits_out_its_retry_after() -> None:
    server = Server(
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json=GOOD),
    )
    result, waits = run_backend(server)
    assert result.intent == "card_arrival"
    assert waits == [1.0]


def test_a_retry_after_longer_than_the_cap_is_not_waited_for() -> None:
    server = Server(httpx.Response(429, headers={"Retry-After": "30"}))
    result, waits = run_backend(server)
    assert isinstance(result, BackendCallError)
    assert server.calls == 1
    assert waits == []


@pytest.mark.parametrize("status", [400, 401, 404, 422, 500])
def test_other_errors_are_not_retried(status) -> None:
    server = Server(httpx.Response(status))
    result, _ = run_backend(server)
    assert isinstance(result, BackendCallError)
    assert not result.transient
    assert server.calls == 1


def test_a_bad_reply_is_not_retried() -> None:
    server = Server(
        httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    )
    result, _ = run_backend(server)
    assert type(result).__name__ == "BackendOutputError"
    assert server.calls == 1


def test_the_last_error_is_raised_after_all_tries() -> None:
    server = Server(httpx.Response(503))
    result, waits = run_backend(server, attempts=3)
    assert isinstance(result, BackendCallError)
    assert str(result) == "Backend returned HTTP 503"
    assert server.calls == 3
    assert len(waits) == 2


def test_a_timeout_that_keeps_happening_ends_as_a_timeout() -> None:
    server = Server(httpx.ReadTimeout("slow"))
    result, _ = run_backend(server, attempts=2)
    assert isinstance(result, BackendTimeoutError)
    assert server.calls == 2


def test_retries_are_off_by_default_and_when_set_to_one() -> None:
    server = Server(httpx.Response(503))
    result, _ = run_backend(server, attempts=1)
    assert isinstance(result, BackendCallError)
    assert server.calls == 1


def test_retries_stay_inside_the_attempt_deadline() -> None:
    """The gateway wraps the whole call in the attempt budget, so retries and
    their waits cannot outlast it."""

    async def go() -> float:
        server = Server(httpx.Response(503))
        config = BackendConfig(name="qwen3-1.7b", route="standard", latency_ms=0)
        async with httpx.AsyncClient(
            base_url="http://vllm.test", transport=httpx.MockTransport(server)
        ) as client:
            backend = VLLMBackend(
                config,
                client,
                "m",
                build_system_prompt(LABELS, {}),
                LABELS,
                retry_attempts=10,
                retry_base_seconds=5,
                retry_max_seconds=5,
            )
            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await backend.generate("hi")
            return loop.time() - start

    assert asyncio.run(go()) < 1.0
