"""Canary releases: the split, the release rules, the API and the store."""

import asyncio
import json
import random
import time
from collections import deque
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.release import (
    CanaryGuard,
    Release,
    ReleaseChange,
    canary_bucket,
    load_versions,
    pick_version,
)
from app_assignment_1.router import MockJevRouter
from app_assignment_1.service import Gateway
from app_assignment_1.store import MemoryStore

TOKEN = "test-operator-token-0123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SMALL = "My card has not arrived"  # routed to qwen3-1.7b


def ids(count: int, seed: int = 0) -> list[UUID]:
    rng = random.Random(seed)
    return [UUID(int=rng.getrandbits(128), version=4) for _ in range(count)]


def release(**fields: object) -> Release:
    values: dict[str, object] = {
        "stable": "v1",
        "reason": "test",
        "changed_at": "2026-11-12T12:00:00Z",
    }
    return Release.model_validate(values | fields)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """v1 is the normal mock config; v2-chaos corrupts every small-model reply.
    Both with no simulated latency."""
    paths = {}
    for name in ("backends", "backends.chaos"):
        backends = json.loads(Path(f"config/{name}.json").read_text())
        for backend in backends:
            backend["latency_ms"] = 0
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(backends))
    versions = tmp_path / "versions.json"
    versions.write_text(
        json.dumps(
            [
                {"id": "v1", "description": "Baseline."},
                {
                    "id": "v2-chaos",
                    "description": "Bad release.",
                    "backends_path": str(paths["backends.chaos"]),
                },
            ]
        )
    )
    return Settings(
        _env_file=None,
        backends_path=paths["backends"],
        versions_path=versions,
        operator_token=TOKEN,
    )


# The split


def test_bucket_is_fixed_for_a_request_id():
    request_id = ids(1)[0]
    assert canary_bucket(request_id) == canary_bucket(UUID(str(request_id)))
    assert all(0 <= canary_bucket(i) < 100 for i in ids(500))


@pytest.mark.parametrize(
    ("request_id", "bucket"),
    [
        # Worked out by hand: SHA-256 of the 16 ID bytes, the first 8 bytes
        # big-endian, mod 100. Hashing fewer bytes or another modulus changes
        # every one of these.
        ("00000000-0000-4000-8000-000000000000", 32),
        ("12345678-1234-4234-8234-123456789abc", 15),
        ("ffffffff-ffff-4fff-bfff-ffffffffffff", 87),
        ("6f1c2a9e-3b4d-4e5f-9a1b-2c3d4e5f6a7b", 82),
    ],
)
def test_known_request_ids_get_known_buckets(request_id: str, bucket: int):
    assert canary_bucket(UUID(request_id)) == bucket


@pytest.mark.parametrize(
    ("percent", "tolerance"),
    # 20,000 fixed IDs. One standard error is 0.07 points at 1% and 99%, and
    # 0.35 points at 50%; each tolerance is a few of those.
    [(1, 0.005), (50, 0.015), (99, 0.005)],
)
def test_the_share_matches_the_percent_at_the_edges(percent: int, tolerance: float):
    current = release(canary="v2", canary_percent=percent)
    share = sum(pick_version(current, i)[1] for i in ids(20_000, seed=84)) / 20_000
    assert abs(share - percent / 100) < tolerance


def test_no_canary_or_zero_percent_sends_everything_to_stable():
    for current in (release(), release(canary="v2", canary_percent=0)):
        assert {pick_version(current, i) for i in ids(200)} == {("v1", False)}


def test_hundred_percent_sends_everything_to_the_canary():
    current = release(canary="v2", canary_percent=100)
    assert {pick_version(current, i) for i in ids(200)} == {("v2", True)}


def test_canary_share_is_close_to_the_percent():
    current = release(canary="v2", canary_percent=10)
    share = sum(pick_version(current, i)[1] for i in ids(10_000)) / 10_000
    # 10,000 draws: one standard error is 0.3 points.
    assert 0.09 < share < 0.11


def test_raising_the_percent_only_moves_stable_requests():
    low = release(canary="v2", canary_percent=10)
    high = release(canary="v2", canary_percent=30)
    for request_id in ids(2000):
        if pick_version(low, request_id)[1]:
            assert pick_version(high, request_id)[1]


# Release rules


@pytest.mark.parametrize(
    "fields",
    [
        {"canary_percent": 10},  # no canary
        {"canary": "v1", "canary_percent": 10},  # canary is stable
        {"canary": "v2", "canary_percent": 101},
        {"canary": "v2", "canary_percent": "10"},  # strict int
        {"canary": "v2", "canary_percent": True},
        {"stable": "V1"},  # ids are lower case
        {"stable": "../v1"},
        {"reason": "   "},
        {"extra": 1},
    ],
)
def test_invalid_release_changes_are_rejected(fields):
    with pytest.raises(ValidationError):
        ReleaseChange.model_validate({"stable": "v1", "reason": "test"} | fields)


def test_versions_must_be_unique_and_present(tmp_path: Path):
    path = tmp_path / "versions.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="at least one"):
        load_versions(path)
    path.write_text(json.dumps([{"id": "v1", "description": "a"}] * 2))
    with pytest.raises(ValueError, match="unique"):
        load_versions(path)


def test_shipped_versions_load():
    versions = load_versions(Path("config/versions.json"))
    assert [v.id for v in versions] == ["v1", "v2-chaos"]
    for version in versions:
        if version.backends_path is not None:
            assert version.backends_path.is_file()


def test_operator_token_settings():
    assert Settings(_env_file=None, operator_token="").operator_token is None
    assert Settings(_env_file=None, canary_version="").canary_version is None
    with pytest.raises(ValidationError, match="16 characters"):
        Settings(_env_file=None, operator_token="short")


def test_startup_rejects_an_unknown_version(settings: Settings):
    bad = settings.model_copy(update={"stable_version": "v9"})
    with pytest.raises(ValueError, match="Unknown version: v9"):
        Gateway(bad, MockJevRouter(bad.intents_path))


def test_canary_from_settings_takes_traffic(settings: Settings):
    canary = settings.model_copy(
        update={"canary_version": "v2-chaos", "canary_percent": 100}
    )
    with TestClient(create_app(canary)) as client:
        trace = client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
    assert trace["version_id"] == "v2-chaos"
    assert trace["canary"] is True


# API


def test_get_release_shows_the_release_and_versions(settings: Settings):
    with TestClient(create_app(settings)) as client:
        body = client.get("/v1/release").json()
    assert body["current"]["stable"] == "v1"
    assert body["current"]["canary"] is None
    assert body["versions"] == [
        {"id": "v1", "description": "Baseline."},
        {"id": "v2-chaos", "description": "Bad release."},
    ]
    assert body["history"] == []


def test_requests_record_the_stable_version(settings: Settings):
    with TestClient(create_app(settings)) as client:
        chat = client.post("/v1/chat", json={"message": SMALL}).json()
        recent = client.get("/v1/health").json()["recent_requests"][0]
        metrics = client.get("/metrics").text
    assert chat["trace"]["version_id"] == "v1"
    assert chat["trace"]["canary"] is False
    assert recent["version_id"] == "v1"
    assert recent["canary"] is False
    assert (
        'gateway_version_requests_total{format_check="passed",'
        'model="qwen3-1.7b",role="stable",version="v1"} 1.0' in metrics
    )


def test_changes_need_the_token(settings: Settings):
    change = {
        "stable": "v1",
        "canary": "v2-chaos",
        "canary_percent": 10,
        "reason": "try it",
    }
    off = settings.model_copy(update={"operator_token": None})
    with TestClient(create_app(off)) as client:
        assert client.put("/v1/release", json=change, headers=AUTH).status_code == 403
    with TestClient(create_app(settings)) as client:
        assert client.put("/v1/release", json=change).status_code == 401
        wrong = {"Authorization": "Bearer not-the-token-at-all"}
        assert client.put("/v1/release", json=change, headers=wrong).status_code == 401
        assert client.get("/v1/release").json()["current"]["canary"] is None


def test_unknown_version_is_a_422(settings: Settings):
    change = {"stable": "v1", "canary": "v9", "canary_percent": 10, "reason": "x"}
    with TestClient(create_app(settings)) as client:
        response = client.put("/v1/release", json=change, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown version: v9"


def test_canary_at_full_share_then_rolled_back(settings: Settings):
    with TestClient(create_app(settings)) as client:
        start = {
            "stable": "v1",
            "canary": "v2-chaos",
            "canary_percent": 100,
            "reason": "Canary",
        }
        response = client.put("/v1/release", json=start, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["canary"] == "v2-chaos"
        bad = client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
        assert (bad["version_id"], bad["canary"]) == ("v2-chaos", True)
        assert bad["format_check"] == "failed"  # the chaos config corrupts it

        back = {"stable": "v1", "reason": "Roll back the canary"}
        assert client.put("/v1/release", json=back, headers=AUTH).status_code == 200
        good = client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
        assert (good["version_id"], good["canary"]) == ("v1", False)
        assert good["format_check"] == "passed"

        history = client.get("/v1/release").json()["history"]
        assert [h["reason"] for h in history] == ["Roll back the canary", "Canary"]


def test_promote_makes_the_canary_stable(settings: Settings):
    with TestClient(create_app(settings)) as client:
        promote = {"stable": "v2-chaos", "reason": "Promote"}
        assert client.put("/v1/release", json=promote, headers=AUTH).status_code == 200
        trace = client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
        health = client.get("/v1/health").json()
    assert (trace["version_id"], trace["canary"]) == ("v2-chaos", False)
    # /v1/health shows the stable version's backends.
    small = next(b for b in health["backends"] if b["name"] == "qwen3-1.7b")
    assert small["fault_rate"] == 1


def test_release_survives_a_restart(settings: Settings):
    store = MemoryStore(100)
    change = {
        "stable": "v1",
        "canary": "v2-chaos",
        "canary_percent": 25,
        "reason": "x",
    }
    with TestClient(create_app(settings, store=store)) as client:
        client.put("/v1/release", json=change, headers=AUTH)
    with TestClient(create_app(settings, store=store)) as client:
        current = client.get("/v1/release").json()["current"]
    assert (current["canary"], current["canary_percent"]) == ("v2-chaos", 25)


def test_a_saved_release_with_a_removed_version_is_ignored(
    settings: Settings, caplog: pytest.LogCaptureFixture
):
    store = MemoryStore(100)
    asyncio.run(store.save_release(release(canary="v9", canary_percent=50)))
    with TestClient(create_app(settings, store=store)) as client:
        current = client.get("/v1/release").json()["current"]
    assert current["canary"] is None
    assert "not configured" in caplog.text


CANARY = {
    "stable": "v1",
    "canary": "v2-chaos",
    "canary_percent": 50,
    "reason": "x",
}


class FlakyReleaseStore(MemoryStore):
    """Release reads fail `read_failures` times, then work. Saves can fail
    before writing, or after writing (a timeout once the row is committed)."""

    def __init__(self, read_failures: int = 0, save: str = "ok") -> None:
        super().__init__(100)
        self.read_failures = read_failures
        self.save = save

    async def save_release(self, release: Release) -> None:
        if self.save == "fail":
            raise ConnectionError("database down")
        await super().save_release(release)
        if self.save == "commit_then_timeout":
            raise TimeoutError

    async def recent_releases(self, limit: int) -> list[Release]:
        if self.read_failures:
            self.read_failures -= 1
            raise ConnectionError("database down")
        return await super().recent_releases(limit)


class SlowReleaseStore(MemoryStore):
    """Writes the release, then holds the save open until `finish` is set,
    like a commit whose reply is slow to arrive."""

    def __init__(self) -> None:
        super().__init__(100)
        self.written = asyncio.Event()
        self.finish = asyncio.Event()

    async def save_release(self, release: Release) -> None:
        await super().save_release(release)
        self.written.set()
        await self.finish.wait()


def test_a_cancel_after_the_store_write_still_applies_the_change(settings: Settings):
    """A client that disconnects mid-PUT cancels the change. If the row was
    already written, the running gateway must apply it too, or it keeps the
    old split while a restart loads the new one."""

    async def main() -> None:
        store = SlowReleaseStore()
        gateway = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        change = ReleaseChange(
            stable="v1", canary="v2-chaos", canary_percent=100, reason="Canary"
        )
        task = asyncio.create_task(gateway.set_release(change))
        await store.written.wait()
        task.cancel()
        await asyncio.sleep(0)
        store.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        live = gateway.release
        assert (live.canary, live.canary_percent) == ("v2-chaos", 100)
        restarted = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        await restarted.load_release()
        assert restarted.release == live

    asyncio.run(main())


def saved_canary_store(read_failures: int) -> FlakyReleaseStore:
    store = FlakyReleaseStore()
    asyncio.run(store.save_release(release(canary="v2-chaos", canary_percent=25)))
    store.read_failures = read_failures
    return store


# Finding 1: a store that cannot be read at startup must not hide a saved
# release (such as a rollback) behind the settings release.


def test_unreadable_store_at_startup_is_shown_then_retried(settings: Settings):
    with TestClient(create_app(settings, store=saved_canary_store(1))) as client:
        gateway = client.app.state.gateway
        assert gateway.release_source == "settings_store_unreadable"
        assert gateway.release.canary is None
        body = client.get("/v1/release").json()
    assert body["source"] == "store"
    assert (body["current"]["canary"], body["current"]["canary_percent"]) == (
        "v2-chaos",
        25,
    )


def test_health_checks_retry_the_release_load(settings: Settings):
    with TestClient(create_app(settings, store=saved_canary_store(1))) as client:
        assert client.get("/v1/health").status_code == 200
        gateway = client.app.state.gateway
        assert gateway.release_source == "store"
        assert gateway.release.canary == "v2-chaos"


def test_release_source_without_a_saved_release(settings: Settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/release").json()["source"] == "settings"
        client.put("/v1/release", json=CANARY, headers=AUTH)
        assert client.get("/v1/release").json()["source"] == "store"


def test_a_store_that_stays_unreadable_is_reported(
    settings: Settings, caplog: pytest.LogCaptureFixture
):
    store = FlakyReleaseStore(read_failures=1000)
    with TestClient(create_app(settings, store=store)) as client:
        body = client.get("/v1/release").json()
        # Chat still works on the settings release.
        assert client.post("/v1/chat", json={"message": SMALL}).status_code == 200
    assert body["source"] == "settings_store_unreadable"
    assert body["history"] == []
    assert "Could not read release history (ConnectionError)" in caplog.text
    assert "database down" not in caplog.text


# Finding 2: a save that fails may still have been committed.


def test_a_save_that_committed_before_a_timeout_is_applied(settings: Settings):
    store = FlakyReleaseStore(save="commit_then_timeout")
    with TestClient(create_app(settings, store=store)) as client:
        response = client.put("/v1/release", json=CANARY, headers=AUTH)
        assert response.status_code == 200
        assert client.app.state.gateway.release.canary == "v2-chaos"
        assert client.get("/v1/release").json()["source"] == "store"


def test_an_unsaved_change_is_not_applied(
    settings: Settings, caplog: pytest.LogCaptureFixture
):
    store = FlakyReleaseStore(save="fail")
    with TestClient(create_app(settings, store=store)) as client:
        response = client.put("/v1/release", json=CANARY, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["detail"] == "The release could not be saved"
        assert client.app.state.gateway.release.canary is None
        assert client.get("/v1/release").json()["source"] == "settings"
    assert "Could not save release (ConnectionError)" in caplog.text
    assert "database down" not in caplog.text


def test_an_unknown_save_outcome_is_reported_and_reloaded(
    settings: Settings, caplog: pytest.LogCaptureFixture
):
    store = FlakyReleaseStore(save="fail")
    with TestClient(create_app(settings, store=store)) as client:
        store.read_failures = 1  # the check after the failed save
        response = client.put("/v1/release", json=CANARY, headers=AUTH)
        assert response.status_code == 503
        assert "may or may not" in response.json()["detail"]
        gateway = client.app.state.gateway
        assert gateway.release.canary is None
        assert gateway.release_source == "settings_store_unreadable"
        # The next read finds nothing saved and settles the state.
        assert client.get("/v1/release").json()["source"] == "settings"
    assert "Could not check the release save (ConnectionError)" in caplog.text


# Finding 3: the token is checked before the body.


@pytest.mark.parametrize("body", [{}, {"stable": "../v1"}, "not json"])
def test_the_token_is_checked_before_the_body(settings: Settings, body: object):
    off = settings.model_copy(update={"operator_token": None})
    with TestClient(create_app(off)) as client:
        response = client.put("/v1/release", json=body, headers=AUTH)
        assert response.status_code == 403
        assert "detail" in response.json()
        assert "loc" not in response.text
    with TestClient(create_app(settings)) as client:
        response = client.put("/v1/release", json=body)
        assert response.status_code == 401
        assert "loc" not in response.text
        # With the token, the body is validated as before.
        assert client.put("/v1/release", json=body, headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    "header",
    [f"bearer {TOKEN}", f"BEARER {TOKEN}", f"Bearer  {TOKEN} "],
)
def test_the_bearer_scheme_is_case_insensitive(settings: Settings, header: str):
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/v1/release", json=CANARY, headers={"Authorization": header}
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header", [TOKEN, f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer", ""]
)
def test_other_authorization_values_are_refused(settings: Settings, header: str):
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/v1/release", json=CANARY, headers={"Authorization": header}
        )
    assert response.status_code == 401


# A failing canary never reaches the controller


def canary_settings(settings: Settings, percent: int) -> Settings:
    return settings.model_copy(
        update={"canary_version": "v2-chaos", "canary_percent": percent}
    )


def wait_for_rollback(client: TestClient) -> dict:
    for _ in range(100):
        current = client.get("/v1/release").json()["current"]
        if current["canary"] is None:
            return current
        time.sleep(0.02)
    raise AssertionError("the canary was not rolled back")


def test_canary_guard_trips_once_when_the_window_fails():
    guard = CanaryGuard(window=10, min_samples=4, threshold=0.5)
    assert [guard.record("v2", True) for _ in range(5)] == [False] * 3 + [True, False]
    guard.reset()
    assert not any(guard.record("v2", False) for _ in range(10))
    # A new canary version starts a fresh window.
    assert guard.record("v3", True) is False
    assert guard.outcomes == deque([True])


def test_a_failing_canary_rolls_itself_back_and_never_disables_stable(
    settings: Settings,
):
    # Every canary reply is corrupt; at 50% the stable model still gets traffic.
    with TestClient(create_app(canary_settings(settings, 50))) as client:
        traces = [
            client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
            for _ in range(60)
        ]
        current = wait_for_rollback(client)
        health = client.get("/v1/health").json()
        after = client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
        history = client.get("/v1/release").json()["history"]

    stable = [t for t in traces if not t["canary"]]
    assert stable
    assert all(t["format_check"] == "passed" for t in stable)
    # The controller saw no canary request: no intervention, nothing disabled.
    assert all(t["controller_action"] is None for t in traces)
    assert health["interventions"] == []
    # Only the canary was rolled back; stable kept serving.
    assert current["stable"] == "v1"
    assert current["canary_percent"] == 0
    assert (after["version_id"], after["canary"]) == ("v1", False)
    assert after["format_check"] == "passed"
    assert history[0]["reason"].startswith("Automatic rollback: canary v2-chaos")


def test_canary_failures_do_not_count_against_the_stable_model(settings: Settings):
    # Without the guard's rollback ever finishing, the controller still sees
    # nothing: even 100% canary traffic leaves the model enabled.
    with TestClient(create_app(canary_settings(settings, 100))) as client:
        for _ in range(40):
            client.post("/v1/chat", json={"message": SMALL})
        health = client.get("/v1/health").json()
    assert health["interventions"] == []


def write_backends(tmp_path: Path, name: str, source: str, **changes: dict) -> Path:
    """A copy of config/<source>.json with no simulated latency and
    `changes` applied per model name."""
    backends = json.loads(Path(f"config/{source}.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
        backend.update(changes.get(backend["name"], {}))
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(backends))
    return path


def test_a_canary_whose_calls_fail_leaves_stable_serving(tmp_path: Path):
    """The repro from the #84 review, through failed calls rather than corrupt
    replies: stable has only its small model, and the canary's small model
    fails every call. Counting the canary against qwen3-1.7b used to disable
    it for stable too, and stable answered 503."""
    stable = write_backends(
        tmp_path, "stable", "backends", **{"qwen3-4b": {"available": False}}
    )
    outage = write_backends(tmp_path, "outage", "backends.outage")
    versions = tmp_path / "versions.json"
    versions.write_text(
        json.dumps(
            [
                {"id": "v1", "description": "Stable."},
                {"id": "v2", "description": "Bad.", "backends_path": str(outage)},
            ]
        )
    )
    settings = Settings(
        _env_file=None,
        backends_path=stable,
        versions_path=versions,
        canary_version="v2",
        canary_percent=50,
    )
    with TestClient(create_app(settings)) as client:
        responses = [
            client.post("/v1/chat", json={"message": SMALL}) for _ in range(40)
        ]
        health = client.get("/v1/health").json()
    assert [r.status_code for r in responses] == [200] * 40
    traces = [r.json()["trace"] for r in responses]
    stable_traces = [t for t in traces if not t["canary"]]
    assert stable_traces
    # Stable answers with its small model; a canary request falls back to the
    # canary's 4b until the canary rolls itself back.
    assert {(t["version_id"], t["model"]) for t in stable_traces} == {
        ("v1", "qwen3-1.7b")
    }
    assert {t["model"] for t in traces if t["canary"]} <= {"qwen3-4b"}
    assert health["interventions"] == []
    monitor = {m["name"]: m for m in health["monitor"]}
    assert not monitor["qwen3-1.7b"]["disabled_by_controller"]
    # The usage /v1/health reports is stable's alone.
    usage = {u["name"]: u for u in health["usage"]}
    assert usage["qwen3-1.7b"]["attempted_calls"] == len(stable_traces)
    assert usage["qwen3-1.7b"]["failed_calls"] == 0
    assert usage["qwen3-4b"]["attempted_calls"] == 0


def test_canary_traffic_does_not_count_toward_a_model_release(settings: Settings):
    """A model release (#107) is watched, then confirmed or rolled back, on
    stable traffic only."""
    with TestClient(create_app(canary_settings(settings, 100))) as client:
        response = client.post(
            "/v1/operator/models/qwen3-1.7b/release",
            json={"version": "v2-faster", "reason": "Watch it"},
            headers=AUTH,
        )
        assert response.status_code == 200
        traces = [
            client.post("/v1/chat", json={"message": SMALL}).json()["trace"]
            for _ in range(10)
        ]
        wait_for_rollback(client)
        health = client.get("/v1/health").json()
    small = next(r for r in health["releases"] if r["name"] == "qwen3-1.7b")
    # Only the requests after the canary's rollback reached the watch.
    stable_calls = sum(not t["canary"] for t in traces)
    assert stable_calls < 10
    assert (small["version"], small["status"], small["watched_samples"]) == (
        "v2-faster",
        "watching",
        stable_calls,
    )
    assert health["interventions"] == []


def test_a_queued_canary_rollback_keeps_a_newer_operator_change(settings: Settings):
    """The canary trips while an operator's change to another canary is being
    saved. The rollback waits for the lock, then finds the canary it judged is
    gone, so it must not remove the operator's new one."""
    versions = json.loads(settings.versions_path.read_text())
    versions.append({"id": "v3", "description": "Next try."})
    settings.versions_path.write_text(json.dumps(versions))
    current = canary_settings(settings, 50)

    async def main() -> None:
        store = SlowReleaseStore()
        gateway = Gateway(current, MockJevRouter(current.intents_path), store=store)
        operator = asyncio.create_task(
            gateway.set_release(
                ReleaseChange(
                    stable="v1", canary="v3", canary_percent=10, reason="Try v3"
                )
            )
        )
        await store.written.wait()
        rollback = asyncio.create_task(
            gateway._roll_back_canary(
                "v2-chaos",
                ReleaseChange(
                    stable="v1",
                    canary=None,
                    canary_percent=0,
                    reason="Automatic rollback: canary v2-chaos kept failing",
                ),
            )
        )
        await asyncio.sleep(0)  # the rollback now waits on the lock
        store.finish.set()
        await operator
        await rollback
        assert (gateway.release.canary, gateway.release.canary_percent) == ("v3", 10)
        saved = await store.recent_releases(5)
        assert [r.reason for r in saved] == ["Try v3"]

    asyncio.run(main())
