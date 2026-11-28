"""Rate limit (429) and input size limits (413, 422): each its own status."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.ratelimit import RateLimiter


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_allows_a_burst_then_says_when_to_retry() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=3, clock=clock)
    assert [limiter.check("a") for _ in range(3)] == [None, None, None]
    assert limiter.check("a") == 1
    clock.now += 1
    assert limiter.check("a") is None
    assert limiter.check("a") == 1


def test_retry_after_rounds_up_to_whole_seconds() -> None:
    limiter = RateLimiter(per_minute=6, burst=1, clock=Clock())
    assert limiter.check("a") is None
    assert limiter.check("a") == 10


def test_clients_have_separate_buckets_and_zero_turns_it_off() -> None:
    limiter = RateLimiter(per_minute=60, burst=1, clock=Clock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None
    off = RateLimiter(per_minute=0, burst=1, clock=Clock())
    assert all(off.check("a") is None for _ in range(100))


def test_old_clients_are_forgotten_past_the_cap(monkeypatch) -> None:
    monkeypatch.setattr("app_assignment_1.ratelimit.MAX_CLIENTS", 3)
    limiter = RateLimiter(per_minute=60, burst=1, clock=Clock())
    for name in "abcd":
        limiter.check(name)
    assert list(limiter.buckets) == ["b", "c", "d"]


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
def test_over_the_limit_is_429_with_retry_after(settings, path) -> None:
    settings.rate_limit_per_minute = 60
    settings.rate_limit_burst = 2
    with TestClient(create_app(settings)) as client:
        codes = [client.post(path, json={"message": "card arrival"}) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    limited = codes[2]
    assert int(limited.headers["Retry-After"]) >= 1
    assert "Too many requests" in limited.json()["detail"]
    assert "X-Request-ID" in limited.headers


def test_health_is_not_rate_limited(settings) -> None:
    settings.rate_limit_per_minute = 60
    settings.rate_limit_burst = 1
    with TestClient(create_app(settings)) as client:
        assert all(client.get("/v1/health").status_code == 200 for _ in range(5))


def test_the_limit_can_be_turned_off(settings) -> None:
    settings.rate_limit_per_minute = 0
    with TestClient(create_app(settings)) as client:
        for _ in range(50):
            assert client.post("/v1/chat", json={"message": "hi"}).status_code == 200


def test_forwarded_for_is_used_only_when_trusted(settings) -> None:
    settings.rate_limit_per_minute = 60
    settings.rate_limit_burst = 1
    one, two = {"X-Forwarded-For": "9.9.9.9, 1.1.1.1"}, {"X-Forwarded-For": "2.2.2.2"}
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/chat", json={"message": "x"}, headers=one).status_code
            == 200
        )
        assert (
            client.post("/v1/chat", json={"message": "x"}, headers=two).status_code
            == 429
        )
    settings.trust_forwarded_for = True
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/chat", json={"message": "x"}, headers=one).status_code
            == 200
        )
        assert (
            client.post("/v1/chat", json={"message": "x"}, headers=two).status_code
            == 200
        )
        # The last entry is what the proxy added; a spoofed first one is ignored.
        spoof = {"X-Forwarded-For": "7.7.7.7, 1.1.1.1"}
        assert (
            client.post("/v1/chat", json={"message": "x"}, headers=spoof).status_code
            == 429
        )


def test_a_body_over_the_limit_is_413(settings) -> None:
    settings.max_body_bytes = 600
    with TestClient(create_app(settings)) as client:
        big = client.post(
            "/v1/chat",
            content=b'{"message":"' + b"a" * 800 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        ok = client.post("/v1/chat", json={"message": "card arrival"})
    assert big.status_code == 413
    assert "too large" in big.json()["detail"]
    assert "600" in big.json()["detail"]
    assert ok.status_code == 200


def test_a_chunked_body_over_the_limit_is_413_too(settings) -> None:
    settings.max_body_bytes = 600

    def body():
        yield b'{"message":"'
        for _ in range(10):
            yield b"a" * 100
        yield b'"}'

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat", content=body(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413


def test_a_message_over_2000_characters_is_422_not_413(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat", json={"message": "a" * 2001})
    assert response.status_code == 422
    assert "at most 2000" in json.dumps(response.json())


def test_the_three_limits_have_three_different_statuses(settings) -> None:
    settings.max_body_bytes = 3000
    settings.rate_limit_per_minute = 60
    settings.rate_limit_burst = 3
    with TestClient(create_app(settings)) as client:
        too_long = client.post("/v1/chat", json={"message": "a" * 2001}).status_code
        too_big = client.post(
            "/v1/chat",
            content=b" " * 4000,
            headers={"Content-Type": "application/json"},
        ).status_code
        for _ in range(3):
            client.post("/v1/chat", json={"message": "hi"})
        limited = client.post("/v1/chat", json={"message": "hi"}).status_code
    assert (too_long, too_big, limited) == (422, 413, 429)


def test_bad_limit_settings_are_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, rate_limit_per_minute=-1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, rate_limit_burst=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_body_bytes=10)
    with pytest.raises(ValueError):
        Settings(_env_file=None, backend_retry_attempts=0)
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            backend_retry_base_seconds=5,
            backend_retry_max_seconds=1,
        )
