"""Model releases: deploy a version, watch it, roll it back (releases.py)."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.releases import AlreadyServing, load_catalog
from app_assignment_1.router import MockJevRouter
from app_assignment_1.schemas import ReleaseEvent
from app_assignment_1.service import BackendUnavailable, Gateway
from app_assignment_1.store import MemoryStore

TOKEN = "operator-token-for-tests-only-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
STANDARD = "my card has not arrived"  # the small model's route
COMPLEX = "my card was stolen"  # the large model's route
CATALOG = {
    SMALL: {
        "v2-good": {"description": "Fine", "changes": {"latency_ms": 1}},
        "v2-bad-format": {"description": "Bad", "changes": {"fault_rate": 1}},
        "v2-outage": {"description": "Down", "changes": {"error_rate": 1}},
        "v2-label-swap": {"description": "Wrong", "changes": {"label_swap_rate": 1}},
    },
    LARGE: {"v2-good": {"description": "Fine", "changes": {"latency_ms": 2}}},
}


def make_settings(tmp_path: Path, catalog=None, **overrides: object) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps(CATALOG if catalog is None else catalog))
    values = {
        "operator_token": TOKEN,
        "controller_cooldown_seconds": 30,
        "monitor_window": 10,
        "monitor_min_samples": 4,
        "release_watch_samples": 6,
    } | overrides
    return Settings(
        _env_file=None,
        backends_path=path,
        releases_path=releases,
        **values,  # type: ignore[arg-type]
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(100)


@pytest.fixture
def client(tmp_path: Path, store: MemoryStore):
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        yield client


def chat(client: TestClient, message: str = STANDARD):
    return client.post("/v1/chat", json={"message": message})


def releases(client: TestClient) -> dict[str, dict]:
    return {r["name"]: r for r in client.get("/v1/health").json()["releases"]}


def monitor(client: TestClient) -> dict[str, dict]:
    return {m["name"]: m for m in client.get("/v1/health").json()["monitor"]}


def deploy(client: TestClient, version: str, name: str = SMALL, **body):
    return client.post(
        f"/v1/operator/models/{name}/release",
        json={"version": version} | body,
        headers=AUTH,
    )


def fault(client: TestClient, kind: str) -> None:
    body = {"model": SMALL, "fault": kind}
    assert client.post("/v1/faults", json=body, headers=AUTH).status_code == 200


def rollback(client: TestClient, name: str = SMALL):
    return client.post(f"/v1/operator/models/{name}/rollback", headers=AUTH)


# --- the catalog ---


def test_every_model_starts_on_base(client):
    small = releases(client)[SMALL]
    assert small["version"] == "base"
    assert small["status"] == "stable"
    assert small["rollback_target"] is None
    assert small["versions"] == [
        "base",
        "v2-good",
        "v2-bad-format",
        "v2-outage",
        "v2-label-swap",
    ]


def test_the_shipped_catalog_loads_and_names_both_models():
    catalog = load_catalog(Path("config/releases.json"))
    assert set(catalog) == {SMALL, LARGE}
    assert all(catalog[SMALL]) and all(catalog[LARGE])


def test_a_missing_catalog_means_base_only(tmp_path):
    assert load_catalog(tmp_path / "none.json") == {}


@pytest.mark.parametrize(
    "catalog",
    [
        {SMALL: {"base": {"description": "x", "changes": {}}}},
        {SMALL: {"V2 Bad": {"description": "x", "changes": {}}}},
        {SMALL: {"v2": {"description": "x", "changes": {"route": "complex"}}}},
        {SMALL: {"v2": {"description": "x", "changes": {"fault_rate": 2}}}},
        {"qwen3-70b": {"v2": {"description": "x", "changes": {}}}},
    ],
)
def test_a_bad_catalog_is_refused(tmp_path, catalog):
    path = tmp_path / "releases.json"
    path.write_text(json.dumps(catalog))
    with pytest.raises((ValueError, ValidationError)):
        load_catalog(path)


# --- deploy, watch, confirm ---


def test_a_good_release_is_watched_then_confirmed(client, store):
    response = deploy(client, "v2-good", reason="faster demo")
    assert response.status_code == 200
    body = response.json()
    assert (body["version"], body["status"], body["rollback_target"]) == (
        "v2-good",
        "watching",
        "base",
    )
    backends = {b["name"]: b for b in client.get("/v1/health").json()["backends"]}
    assert backends[SMALL]["latency_ms"] == 1
    for _ in range(5):
        assert chat(client).status_code == 200
    assert releases(client)[SMALL]["watched_samples"] == 5
    assert releases(client)[SMALL]["status"] == "watching"
    chat(client)
    small = releases(client)[SMALL]
    assert (small["status"], small["rollback_target"]) == ("stable", "base")
    assert [(e.action, e.actor, e.version) for e in store.release_events] == [
        ("deployed", "operator", "v2-good"),
        ("confirmed", "controller", "v2-good"),
    ]
    history = client.get("/v1/health").json()["release_history"]
    assert [e["action"] for e in history] == ["deployed", "confirmed"]
    assert history[0]["reason"] == "faster demo"


def test_a_bad_release_is_rolled_back_not_disabled(client, store):
    deploy(client, "v2-bad-format")
    traces = [chat(client).json()["trace"] for _ in range(4)]
    assert [t["format_check"] for t in traces] == ["failed"] * 4
    assert traces[-1]["controller_action"] == (
        "rolled back qwen3-1.7b to base: format failure rate 100% over last 4 calls"
    )
    small = releases(client)[SMALL]
    assert (small["version"], small["status"], small["rollback_target"]) == (
        "base",
        "rolled_back",
        None,
    )
    stats = monitor(client)[SMALL]
    assert not stats["disabled_by_controller"]
    assert stats["samples"] == 0  # a fresh window for base
    # The model serves its own route again, and its replies pass.
    trace = chat(client).json()["trace"]
    assert (trace["model"], trace["format_check"]) == (SMALL, "passed")
    interventions = client.get("/v1/health").json()["interventions"]
    assert [(i["backend"], i["action"]) for i in interventions] == [
        (SMALL, "rolled_back")
    ]
    assert [(e.action, e.actor) for e in store.release_events] == [
        ("deployed", "operator"),
        ("rolled_back", "controller"),
    ]


def test_a_rollback_also_saves_the_last_usable_model(client):
    """The controller never disables the last usable model, but it can roll
    one back: the outage ends instead of lasting until an operator acts."""
    hold = client.post(f"/v1/operator/models/{LARGE}/disable", headers=AUTH)
    assert hold.status_code == 200
    deploy(client, "v2-outage")
    codes = [chat(client).status_code for _ in range(4)]
    assert codes == [503] * 4
    assert releases(client)[SMALL]["status"] == "rolled_back"
    assert chat(client).status_code == 200


def test_without_a_release_the_controller_still_disables(client):
    """A fault on a confirmed or base release is not the release's fault."""
    client.post(
        "/v1/faults", json={"model": SMALL, "fault": "bad_format"}, headers=AUTH
    )
    for _ in range(4):
        chat(client)
    assert monitor(client)[SMALL]["disabled_by_controller"]
    assert releases(client)[SMALL]["version"] == "base"


def test_a_failure_after_the_watch_disables_as_usual(client):
    deploy(client, "v2-good")
    for _ in range(6):
        chat(client)
    assert releases(client)[SMALL]["status"] == "stable"
    client.post(
        "/v1/faults", json={"model": SMALL, "fault": "bad_format"}, headers=AUTH
    )
    # Six passing calls are still in the window: 5 of 10 failed crosses 50%.
    for _ in range(5):
        chat(client)
    assert monitor(client)[SMALL]["disabled_by_controller"]
    assert releases(client)[SMALL]["version"] == "v2-good"


def test_a_deploy_starts_a_fresh_window(client):
    """Failures of the version before must not roll back the new one."""
    client.post(
        "/v1/faults",
        json={"model": SMALL, "fault": "bad_format", "rate": 1},
        headers=AUTH,
    )
    for _ in range(3):
        chat(client)
    assert monitor(client)[SMALL]["samples"] == 3
    deploy(client, "v2-good")
    assert monitor(client)[SMALL]["samples"] == 0
    # The deploy replaced the fault: a version is the whole config.
    trace = chat(client).json()["trace"]
    assert trace["format_check"] == "passed"


def test_the_watch_is_never_shorter_than_min_samples(tmp_path):
    settings = make_settings(tmp_path, monitor_min_samples=8, release_watch_samples=3)
    with TestClient(create_app(settings, store=MemoryStore(100))) as client:
        assert releases(client)[SMALL]["watch_samples"] == 8


# --- the rollback target ---


def test_an_unconfirmed_release_is_no_rollback_target(client):
    deploy(client, "v2-good")
    response = deploy(client, "v2-bad-format")
    assert response.json()["rollback_target"] == "base"
    for _ in range(4):
        chat(client)
    assert releases(client)[SMALL]["version"] == "base"


def test_a_confirmed_release_becomes_the_rollback_target(client):
    deploy(client, "v2-good")
    for _ in range(6):
        chat(client)
    deploy(client, "v2-bad-format")
    for _ in range(4):
        chat(client)
    small = releases(client)[SMALL]
    assert (small["version"], small["status"]) == ("v2-good", "rolled_back")


def test_deploying_the_target_during_a_watch_is_a_rollback(client):
    """Review of #107, item 3: the next deploy still has a target, so a bad
    release after it is rolled back, not disabled."""
    deploy(client, "v2-good")
    response = deploy(client, "base")
    body = response.json()
    assert (body["version"], body["status"], body["rollback_target"]) == (
        "base",
        "rolled_back",
        None,
    )
    assert deploy(client, "v2-bad-format").json()["rollback_target"] == "base"
    actions = [chat(client).json()["trace"]["controller_action"] for _ in range(4)]
    assert actions[-1].startswith("rolled back qwen3-1.7b to base")
    assert releases(client)[SMALL]["status"] == "rolled_back"
    assert not monitor(client)[SMALL]["disabled_by_controller"]


def test_deploying_a_confirmed_target_is_watched(client):
    deploy(client, "v2-good")
    for _ in range(6):
        chat(client)
    body = deploy(client, "base").json()
    assert (body["status"], body["rollback_target"]) == ("watching", "v2-good")


# --- operator routes ---


def test_operator_rollback(client):
    assert rollback(client).status_code == 409
    deploy(client, "v2-bad-format")
    response = rollback(client)
    assert response.status_code == 200
    assert (response.json()["version"], response.json()["status"]) == (
        "base",
        "rolled_back",
    )
    assert chat(client).json()["trace"]["format_check"] == "passed"
    assert rollback(client).status_code == 409


def test_unknown_or_current_versions_are_refused(client):
    assert deploy(client, "v9").status_code == 404
    assert deploy(client, "v2-label-swap", name=LARGE).status_code == 404
    assert deploy(client, "base").status_code == 409
    assert deploy(client, "Not A Version").status_code == 422
    assert deploy(client, "v2-good", reason="").status_code == 422


@pytest.mark.parametrize("path", ["release", "rollback"])
def test_release_routes_need_the_token(client, tmp_path, path):
    url = f"/v1/operator/models/{SMALL}/{path}"
    body = {"version": "v2-good"}
    assert client.post(url, json=body).status_code == 401
    wrong = {"Authorization": "Bearer wrong-token-wrong-token"}
    assert client.post(url, json=body, headers=wrong).status_code == 401
    settings = make_settings(tmp_path, operator_token=None)
    with TestClient(create_app(settings, store=MemoryStore(10))) as off:
        assert off.post(url, json=body, headers=AUTH).status_code == 403


class FailingReleaseStore(MemoryStore):
    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        raise ConnectionError("database down")


def test_a_change_that_cannot_be_saved_is_not_made(tmp_path):
    store = FailingReleaseStore(100)
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        response = deploy(client, "v2-bad-format")
        assert response.status_code == 503
        assert releases(client)[SMALL]["version"] == "base"
        assert chat(client).json()["trace"]["format_check"] == "passed"


class SlowFailingStore(MemoryStore):
    """Saves operator events, fails the controller's: they must wait."""

    def __init__(self) -> None:
        super().__init__(100)
        self.fail = False

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        if self.fail:
            raise ConnectionError("database down")
        await super().save_release_events(items)


def test_a_controller_rollback_waits_for_the_store(tmp_path):
    store = SlowFailingStore()
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        deploy(client, "v2-bad-format")
        store.fail = True
        for _ in range(4):
            chat(client)
        assert releases(client)[SMALL]["status"] == "rolled_back"
        gateway = client.app.state.gateway  # type: ignore[attr-defined]
        assert [e.action for e in gateway.releases.unsaved] == ["rolled_back"]
        store.fail = False
        chat(client)
        assert gateway.releases.unsaved == []
        assert [e.action for e in store.release_events] == ["deployed", "rolled_back"]


class PausedReleaseStore(MemoryStore):
    """Pause an operator write while the previous release's last call finishes."""

    def __init__(self) -> None:
        super().__init__(100)
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        if any(event.version == "v2-outage" for event in items):
            self.entered.set()
            await self.resume.wait()
        await super().save_release_events(items)


def test_an_operator_ack_matches_storage_when_a_call_finishes_during_save(tmp_path):
    async def main():
        store = PausedReleaseStore()
        settings = make_settings(tmp_path)
        gateway = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        await gateway.deploy_release(SMALL, "v2-good", "first deploy")
        for _ in range(5):
            await gateway.chat(STANDARD)
        backend = gateway.backend(SMALL)
        original = backend.generate
        entered, resume, finishing = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def generate(message):
            reply = await original(message)
            entered.set()
            await resume.wait()
            finishing.set()
            return reply

        backend.generate = generate
        old_chat = asyncio.create_task(gateway.chat(STANDARD))
        await asyncio.wait_for(entered.wait(), 1)
        deploy = asyncio.create_task(
            gateway.deploy_release(SMALL, "v2-outage", "concurrent deploy")
        )
        await asyncio.wait_for(store.entered.wait(), 1)
        resume.set()
        await asyncio.wait_for(finishing.wait(), 1)
        try:
            # The old call confirms v2-good during the operator's write: the
            # chat path never waits for that write.
            assert gateway.releases.state[SMALL].status == "stable"
        finally:
            store.resume.set()
        ack = await asyncio.wait_for(deploy, 1)
        # The deploy was planned again on the confirmed state, with a newer
        # revision than the confirmation, and saved before the answer.
        saved = (await store.latest_release_events())[0]
        assert (ack.version, ack.rollback_target) == (
            saved.version,
            saved.rollback_target,
        )
        assert saved.rollback_target == "v2-good"
        assert (await old_chat).trace.simulated_latency_ms == 1
        await gateway.finish_saves()
        revisions = {e.action: e.revision for e in list(store.release_events)[-3:]}
        stale, again = (e for e in store.release_events if e.version == "v2-outage")
        assert stale.revision < revisions["confirmed"] < again.revision
        assert (await store.latest_release_events())[0] == again
        assert gateway.releases.state[SMALL].watched_samples == 0

    asyncio.run(main())


def test_waiting_controller_events_are_saved_before_a_deploy(tmp_path):
    """Review of #107, item 2: a rollback whose save failed must not be
    saved after a newer deploy, or a restart would undo the deploy."""
    store = SlowFailingStore()
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, store=store)) as client:
        deploy(client, "v2-bad-format")
        store.fail = True
        for _ in range(4):
            chat(client)
        assert releases(client)[SMALL]["status"] == "rolled_back"
        store.fail = False
        deploy(client, "v2-good")
        chat(client)
        # Written out of order, but the revisions keep the gateway's order.
        assert sorted(
            (e.revision, e.action, e.version) for e in store.release_events
        ) == [
            (1, "deployed", "v2-bad-format"),
            (2, "rolled_back", "base"),
            (3, "deployed", "v2-good"),
        ]
    with TestClient(create_app(settings, store=store)) as client:
        small = releases(client)[SMALL]
        assert (small["version"], small["status"]) == ("v2-good", "watching")


def test_a_failed_deploy_save_keeps_waiting_events(tmp_path):
    store = SlowFailingStore()
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        deploy(client, "v2-bad-format")
        store.fail = True
        for _ in range(4):
            chat(client)
        assert deploy(client, "v2-good").status_code == 503
        gateway = client.app.state.gateway  # type: ignore[attr-defined]
        # The controller's rollback, then the release the model kept.
        assert [(e.action, e.actor) for e in gateway.releases.unsaved] == [
            ("rolled_back", "controller"),
            ("rolled_back", "operator"),
        ]
        assert releases(client)[SMALL]["version"] == "base"


# --- restart ---


def test_a_confirmed_release_runs_its_config_after_a_restart(tmp_path):
    """Review of #107, item 1."""
    store = MemoryStore(100)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, store=store)) as client:
        deploy(client, "v2-good")
        for _ in range(6):
            chat(client)
        assert releases(client)[SMALL]["status"] == "stable"
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == "v2-good"
        backends = {b["name"]: b for b in client.get("/v1/health").json()["backends"]}
        assert backends[SMALL]["latency_ms"] == 1


def test_a_restart_keeps_the_release(tmp_path):
    store = MemoryStore(100)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, store=store)) as client:
        deploy(client, "v2-good")
        for _ in range(6):
            chat(client)
        deploy(client, "v2-bad-format", name=SMALL)
        rollback(client)
        deploy(client, "v2-good", name=LARGE)
    with TestClient(create_app(settings, store=store)) as client:
        state = releases(client)
        assert (state[SMALL]["version"], state[SMALL]["status"]) == (
            "v2-good",
            "rolled_back",
        )
        # A release still under watch is watched again from zero.
        assert (state[LARGE]["version"], state[LARGE]["status"]) == (
            "v2-good",
            "watching",
        )
        assert state[LARGE]["watched_samples"] == 0
        backends = {b["name"]: b for b in client.get("/v1/health").json()["backends"]}
        assert backends[LARGE]["latency_ms"] == 2
        # Restoring writes nothing new.
        assert len(store.release_events) == 5


def test_a_saved_version_gone_from_the_catalog_starts_on_base(tmp_path, caplog):
    store = MemoryStore(100)
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        deploy(client, "v2-good")
    other = tmp_path / "other"
    other.mkdir()
    settings = make_settings(other, catalog={})
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == "base"
    assert "not in the catalog" in caplog.text


class BrokenReadStore(MemoryStore):
    async def latest_release_events(self) -> list[ReleaseEvent]:
        raise ConnectionError("database down")


def test_a_store_problem_at_startup_starts_on_base(tmp_path, caplog):
    store = BrokenReadStore(100)
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        assert releases(client)[SMALL]["version"] == "base"
        assert chat(client).status_code == 200
    assert "Could not load releases (ConnectionError)" in caplog.text


# --- semantic actions and metrics ---


def test_a_wrong_intent_release_is_rolled_back(tmp_path):
    settings = make_settings(
        tmp_path,
        semantic_window=6,
        semantic_min_samples=3,
        semantic_baseline_disagreement_rate=0.0,
        semantic_alert_margin=0.2,
        semantic_actions_enabled=True,
    )
    with TestClient(create_app(settings, store=MemoryStore(100))) as client:
        deploy(client, "v2-label-swap")
        traces = [chat(client).json()["trace"] for _ in range(3)]
        assert traces[-1]["controller_action"].startswith(
            "rolled back qwen3-1.7b to base: intent differs from Jev's"
        )
        assert releases(client)[SMALL]["status"] == "rolled_back"
        semantic = {s["name"]: s for s in client.get("/v1/health").json()["semantic"]}
        assert semantic[SMALL]["samples"] == 0  # a fresh window for base
        assert not monitor(client)[SMALL]["disabled_by_controller"]


def test_wrong_intents_do_not_roll_back_with_actions_off(client):
    deploy(client, "v2-label-swap")
    for _ in range(6):
        chat(client)
    assert releases(client)[SMALL]["status"] == "stable"


def test_metrics_show_each_models_release(client):
    deploy(client, "v2-bad-format")
    for _ in range(4):
        chat(client)
    lines = client.get("/metrics").text.splitlines()
    release = 'gateway_model_release{model="%s",status="%s",version="base"} 1.0'
    assert release % (SMALL, "rolled_back") in lines
    assert release % (LARGE, "stable") in lines
    action = 'gateway_controller_interventions_total{action="rolled_back",model="%s"}'
    assert f"{action % SMALL} 1.0" in lines


# --- calls that span a change, rerouted calls, the semantic minimum ---


def gateway_for(settings: Settings) -> Gateway:
    return Gateway(settings, MockJevRouter(settings.intents_path))


def test_calls_started_on_the_old_version_do_not_count(tmp_path):
    """Review of #107, item 4: base fails every call, four slow calls are in
    flight, and a good release is deployed. Their failures belong to base."""
    g = gateway_for(make_settings(tmp_path))
    small = g.backend(SMALL)
    small.config.error_rate = 1
    small.config.latency_ms = 200
    g.backend(LARGE).config.available = False

    async def main() -> list[int]:
        calls = [asyncio.create_task(g.chat(STANDARD)) for _ in range(4)]
        await asyncio.sleep(0.05)
        await g.deploy_release(SMALL, "v2-good", "fix during an incident")
        results = await asyncio.gather(*calls, return_exceptions=True)
        return [0 if isinstance(r, BackendUnavailable) else 1 for r in results]

    assert asyncio.run(main()) == [0, 0, 0, 0]
    state = g.releases.state[SMALL]
    assert (state.version, state.status) == ("v2-good", "watching")
    assert len(g.controller.windows[SMALL]) == 0
    assert g.usage[SMALL].failed_calls == 4


def test_rerouted_calls_do_not_confirm_a_release(tmp_path):
    """Review of #107, item 5: with the large model held, complex messages go
    to the small model, where the semantic check never sees them."""
    settings = make_settings(
        tmp_path,
        semantic_window=6,
        semantic_min_samples=3,
        semantic_baseline_disagreement_rate=0.0,
        semantic_alert_margin=0.2,
        semantic_actions_enabled=True,
    )
    with TestClient(create_app(settings, store=MemoryStore(100))) as client:
        client.post(f"/v1/operator/models/{LARGE}/disable", headers=AUTH)
        deploy(client, "v2-label-swap")
        for _ in range(8):
            assert chat(client, COMPLEX).json()["trace"]["model"] == SMALL
        small = releases(client)[SMALL]
        assert (small["status"], small["watched_samples"]) == ("watching", 0)
        for _ in range(3):
            chat(client)
        assert releases(client)[SMALL]["status"] == "rolled_back"


@pytest.mark.parametrize("min_samples", [5, 6])
def test_the_watch_gives_the_semantic_check_its_samples(tmp_path, min_samples):
    """Review of #107, item 6: the semantic check on the watch's last call
    comes before the release is confirmed."""
    settings = make_settings(
        tmp_path,
        semantic_window=6,
        semantic_min_samples=min_samples,
        semantic_baseline_disagreement_rate=0.0,
        semantic_alert_margin=0.2,
        semantic_actions_enabled=True,
        release_watch_samples=6,
    )
    with TestClient(create_app(settings, store=MemoryStore(100))) as client:
        deploy(client, "v2-label-swap")
        for _ in range(6):
            chat(client)
        small = releases(client)[SMALL]
        assert (small["version"], small["status"]) == ("base", "rolled_back")


def test_the_watch_is_never_shorter_than_the_semantic_minimum(tmp_path):
    settings = make_settings(
        tmp_path, semantic_min_samples=9, semantic_actions_enabled=True
    )
    assert gateway_for(settings).controller.watch_samples == 9
    off = make_settings(tmp_path, semantic_min_samples=9)
    assert gateway_for(off).controller.watch_samples == 6


def test_semantic_watch_waits_for_comparable_intent_evidence(tmp_path):
    settings = make_settings(
        tmp_path,
        semantic_window=6,
        semantic_min_samples=3,
        semantic_baseline_disagreement_rate=0.0,
        semantic_actions_enabled=True,
        jev_breaker_failures=30,
    )

    class FailingJev(MockJevRouter):
        async def route(self, message):
            raise ConnectionError("test outage")

    with TestClient(create_app(settings, FailingJev(settings.intents_path))) as client:
        deploy(client, "v2-label-swap")
        for _ in range(6):
            assert chat(client).status_code == 200
        assert releases(client)[SMALL]["status"] == "watching"


# --- second review: the reply path, cancels and saves that answer late ---


class BlockingStore(MemoryStore):
    """Holds every release write until `resume` is set."""

    def __init__(self) -> None:
        super().__init__(100)
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        self.entered.set()
        await self.resume.wait()
        await super().save_release_events(items)


def test_an_operator_save_never_delays_a_chat(tmp_path):
    """Review 2 of #107, item 1: chats on both models answer while a deploy
    is still being saved, within a short deadline."""

    async def main() -> None:
        store = BlockingStore()
        settings = make_settings(
            tmp_path,
            request_deadline_seconds=1.0,
            alternate_reserve_seconds=0.5,
            jev_timeout_seconds=0.2,
        )
        g = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        deploy = asyncio.create_task(g.deploy_release(LARGE, "v2-good", "slow save"))
        await asyncio.wait_for(store.entered.wait(), 1)
        small, large = await asyncio.wait_for(
            asyncio.gather(g.chat(STANDARD), g.chat(COMPLEX)), 0.5
        )
        assert (small.trace.model, large.trace.model) == (SMALL, LARGE)
        # Failover to the other model still works during the save.
        g.backend(SMALL).config.error_rate = 1
        failover = await asyncio.wait_for(g.chat(STANDARD), 0.5)
        assert failover.trace.model == LARGE
        assert not deploy.done()
        store.resume.set()
        assert (await deploy).version == "v2-good"

    asyncio.run(main())


def test_two_cancels_keep_the_lock_until_the_save_ends(tmp_path):
    """Review 2 of #107, item 2."""

    async def main() -> None:
        store = BlockingStore()
        settings = make_settings(tmp_path)
        g = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        deploy = asyncio.create_task(g.deploy_release(SMALL, "v2-good", "cancel me"))
        await asyncio.wait_for(store.entered.wait(), 1)
        deploy.cancel()
        await asyncio.sleep(0)
        deploy.cancel()
        await asyncio.sleep(0.05)
        assert g.release_lock.locked()
        assert not deploy.done()
        store.resume.set()
        with pytest.raises(asyncio.CancelledError):
            await deploy
        assert not g.release_lock.locked()
        # The saved change was applied, so the same deploy is refused.
        assert g.releases.state[SMALL].version == "v2-good"
        with pytest.raises(AlreadyServing):
            await g.deploy_release(SMALL, "v2-good", "again")
        assert [e.version for e in store.release_events] == ["v2-good"]

    asyncio.run(main())


class LateStore(MemoryStore):
    """Saves, then answers after the store timeout."""

    def __init__(self, readable: bool = True) -> None:
        super().__init__(100)
        self.readable = readable
        self.late = True

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        await super().save_release_events(items)
        if self.late:
            await asyncio.sleep(1)

    async def latest_release_events(self) -> list[ReleaseEvent]:
        if not self.readable:
            raise ConnectionError("database down")
        return await super().latest_release_events()


def test_a_save_that_answers_late_is_read_back(tmp_path):
    """Review 2 of #107, item 3: the write committed, so the change applies."""
    store = LateStore()
    settings = make_settings(tmp_path, store_timeout_seconds=0.1)
    with TestClient(create_app(settings, store=store)) as client:
        response = deploy(client, "v2-bad-format")
        assert response.status_code == 200
        assert releases(client)[SMALL]["version"] == "v2-bad-format"
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == "v2-bad-format"


def test_an_unconfirmed_save_agrees_live_and_after_a_restart(tmp_path):
    """Review 3 of #107: the write lands but answers late, and the read-back
    fails. The operator is told the change was not made; the running gateway
    keeps the old release; and once the store answers, a restart keeps it too,
    though the store holds the disputed change."""
    store = LateStore()
    settings = make_settings(tmp_path, store_timeout_seconds=0.1)
    with TestClient(create_app(settings, store=store)) as client:
        store.readable = False  # after the startup read
        response = deploy(client, "v2-bad-format")
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "so it was not made" in detail
        # Review 4: the kept release is only in memory until the store
        # answers, so the answer says a restart may serve the change.
        assert "Until then, a restart may serve the change" in detail
        assert releases(client)[SMALL]["version"] == "base"
        # The disputed change is in the store, below the kept release.
        saved = [(e.version, e.revision) for e in store.release_events]
        assert saved[0] == ("v2-bad-format", 1)
        store.readable = True
        store.late = False
        chat(client)  # the store answers again; the kept release is saved
        assert releases(client)[SMALL]["version"] == "base"
    with TestClient(create_app(settings, store=store)) as client:
        small = releases(client)[SMALL]
        assert (small["version"], small["status"]) == ("base", "stable")
        assert chat(client).json()["trace"]["format_check"] == "passed"
    latest = asyncio.run(store.latest_release_events())
    assert [(e.version, e.actor, e.revision) for e in latest] == [
        ("base", "operator", 2)
    ]


class OneModelBlockingStore(MemoryStore):
    """Holds release writes for the large model until `resume` is set."""

    def __init__(self) -> None:
        super().__init__(100)
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        if any(e.backend == LARGE for e in items):
            self.entered.set()
            await self.resume.wait()
        await super().save_release_events(items)


def test_a_rollback_chat_finishes_during_another_models_operator_save(tmp_path):
    """Review 3 of #107: the chat that rolls the small model back, and its
    saves, finish while a deploy to the large model is still being saved."""

    async def main() -> None:
        store = OneModelBlockingStore()
        settings = make_settings(tmp_path)
        g = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        await g.deploy_release(SMALL, "v2-bad-format", "bad release")
        pending = asyncio.create_task(g.deploy_release(LARGE, "v2-good", "slow"))
        await asyncio.wait_for(store.entered.wait(), 1)
        traces = [
            (await asyncio.wait_for(g.chat(STANDARD), 0.5)).trace for _ in range(4)
        ]
        assert traces[-1].controller_action.startswith("rolled back qwen3-1.7b to base")
        assert g.releases.state[SMALL].status == "rolled_back"
        # The rollback and its intervention are saved, not stuck behind it.
        assert g.releases.unsaved == []
        assert [i.action for i in store.interventions] == ["rolled_back"]
        assert not pending.done()
        store.resume.set()
        assert (await pending).version == "v2-good"

    asyncio.run(main())


class UnreadableStore(MemoryStore):
    """Release reads fail while `readable` is False."""

    def __init__(self) -> None:
        super().__init__(100)
        self.readable = True

    async def latest_release_events(self) -> list[ReleaseEvent]:
        if not self.readable:
            raise ConnectionError("database down")
        return await super().latest_release_events()


def test_release_changes_wait_for_the_saved_releases_to_be_read(tmp_path):
    """Review 4 of #107, item 1: when the startup read fails, the revision
    counter is unknown. A change numbered from 0 would reuse a saved
    revision, so changes are refused until a read works; then the saved
    release is served and the change is numbered after it."""
    store = UnreadableStore()
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, store=store)) as client:
        assert deploy(client, "v2-good").status_code == 200
    store.readable = False
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == "base"
        response = deploy(client, "v2-label-swap")
        assert response.status_code == 503
        assert "could not be read" in response.json()["detail"]
        assert rollback(client).status_code == 503
        store.readable = True
        response = deploy(client, "v2-label-swap")
        assert response.status_code == 200
        assert response.json()["rollback_target"] == "base"
        live = releases(client)[SMALL]["version"]
    assert live == "v2-label-swap"
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == live
    latest = asyncio.run(store.latest_release_events())
    assert [(e.version, e.revision) for e in latest] == [("v2-label-swap", 2)]


def test_a_revision_another_gateway_took_is_not_a_success(tmp_path):
    """Review 4 of #107, item 1: two gateways on one store, as in a rolling
    restart, each count revisions. The second gateway's change under a taken
    revision is refused, and its next one is numbered after it."""
    store = MemoryStore(100)
    settings = make_settings(tmp_path)
    with (
        TestClient(create_app(settings, store=store)) as first,
        TestClient(create_app(settings, store=store)) as second,
    ):
        assert deploy(first, "v2-good").status_code == 200
        response = deploy(second, "v2-label-swap")
        assert response.status_code == 503
        assert "nothing changed" in response.json()["detail"]
        assert releases(second)[SMALL]["version"] == "base"
        assert deploy(second, "v2-label-swap").status_code == 200
    with TestClient(create_app(settings, store=store)) as client:
        assert releases(client)[SMALL]["version"] == "v2-label-swap"
    saved = [(e.version, e.revision) for e in store.release_events]
    assert saved == [("v2-good", 1), ("v2-label-swap", 2)]
