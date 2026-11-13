"""Canary releases: the split, the release rules, the API and the store."""

import asyncio
import json
import random
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.release import (
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
        'gateway_version_requests_total{format_check="passed",role="stable",'
        'version="v1"} 1.0' in metrics
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
