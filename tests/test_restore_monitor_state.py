"""After a restart, the gateway rebuilds its monitor windows and running
cooldowns from the store."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app_assignment_1.config import Settings
from app_assignment_1.monitor import Controller
from app_assignment_1.router import MockJevRouter
from app_assignment_1.schemas import (
    KEPT_REASON_MARKER,
    Intervention,
    MonitorSample,
    ReleaseEvent,
)
from app_assignment_1.service import Gateway
from app_assignment_1.store import MemoryStore

STANDARD = "my card has not arrived"
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def gateway(settings: Settings, store: MemoryStore) -> Gateway:
    return Gateway(settings, MockJevRouter(settings.intents_path), store=store)


def chat(g: Gateway, times: int, message: str = STANDARD) -> None:
    async def go() -> None:
        for _ in range(times):
            await g.chat(message)

    asyncio.run(go())


def failure(at: datetime, outcome: str = "ok", model: str = SMALL) -> MonitorSample:
    return MonitorSample(at=at, model=model, monitor="failure", outcome=outcome)


def event(at: datetime, action: str, model: str = SMALL) -> Intervention:
    return Intervention(at=at, backend=model, action=action, reason="test")


def test_a_restart_rebuilds_windows_and_resumes_the_cooldown():
    # The chaos config corrupts every small-model reply, so 5 standard
    # messages disable it. A second gateway on the same store is a restart.
    settings = Settings(
        _env_file=None, backends_path=Path("config/backends.chaos.json")
    )
    store = MemoryStore(settings.request_store_limit)
    first = gateway(settings, store)
    chat(first, 5)
    assert first.controller.is_disabled(SMALL)
    before = {m.name: m for m in first.controller.stats()}
    semantic_before = first.semantic.stats()

    second = gateway(settings, store)
    asyncio.run(second.restore())
    after = {m.name: m for m in second.controller.stats()}
    assert after[SMALL].disabled_by_controller
    assert after[SMALL].disabled_for_seconds is not None
    assert 55 < after[SMALL].disabled_for_seconds <= 60
    assert after[SMALL].format_failures == before[SMALL].format_failures == 5
    assert after[LARGE].samples == before[LARGE].samples
    assert second.semantic.stats() == semantic_before
    # Restored samples are not saved a second time.
    assert second.controller.unsaved_samples == []
    assert second.semantic.unsaved == []


def test_only_samples_after_the_last_re_enable_come_back(tmp_path):
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    samples = [
        failure(NOW - timedelta(minutes=10), "format_failure"),
        failure(NOW - timedelta(minutes=9), "format_failure"),
        failure(NOW - timedelta(minutes=2), "ok"),
    ]
    events = [
        event(NOW - timedelta(minutes=8), "disabled"),
        event(NOW - timedelta(minutes=7), "re-enabled"),
    ]
    c.restore(events, samples, NOW)
    assert list(c.windows[SMALL]) == ["ok"]
    assert not c.is_disabled(SMALL)


def test_a_cooldown_that_ended_while_stopped_starts_an_empty_window():
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    samples = [failure(NOW - timedelta(minutes=5), "format_failure")] * 5
    # Disabled 2 minutes ago with a 60 s cooldown: it would have been
    # re-enabled, which clears the window.
    c.restore([event(NOW - timedelta(minutes=2), "disabled")], samples, NOW)
    assert not c.is_disabled(SMALL)
    assert list(c.windows[SMALL]) == []


def test_a_running_cooldown_keeps_the_window_from_before_the_disable():
    clock = [100.0]
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: clock[0])
    samples = [failure(NOW - timedelta(seconds=40), "format_failure")] * 5
    c.restore([event(NOW - timedelta(seconds=20), "disabled")], samples, NOW)
    assert c.disabled_until[SMALL] == 140.0  # 40 s of the 60 s cooldown left
    assert list(c.windows[SMALL]) == ["format_failure"] * 5
    clock[0] = 141.0
    assert not c.is_disabled(SMALL)


def test_an_operator_hold_comes_back_after_a_restart():
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    events = [
        event(NOW - timedelta(hours=2), "operator_disabled"),
        event(NOW - timedelta(hours=1), "operator_enabled"),
        event(NOW - timedelta(minutes=30), "operator_disabled"),
        event(NOW - timedelta(minutes=20), "operator_disabled", LARGE),
        event(NOW - timedelta(minutes=10), "operator_enabled", LARGE),
    ]
    c.restore(events, [], NOW)
    assert c.held == {SMALL}
    assert c.is_disabled(SMALL)
    assert not c.is_disabled(LARGE)
    # Restoring logs nothing new.
    assert c.unsaved == []


def test_an_operator_enable_ends_the_cooldown_and_clears_the_window():
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    samples = [
        failure(NOW - timedelta(seconds=40), "format_failure"),
        failure(NOW - timedelta(seconds=5), "ok"),
    ]
    events = [
        event(NOW - timedelta(seconds=30), "disabled"),
        event(NOW - timedelta(seconds=20), "operator_enabled"),
    ]
    c.restore(events, samples, NOW)
    assert not c.is_disabled(SMALL)
    assert list(c.windows[SMALL]) == ["ok"]


def test_a_disable_after_an_operator_enable_keeps_the_newer_samples():
    clock = [100.0]
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: clock[0])
    samples = [
        failure(NOW - timedelta(minutes=5), "call_error"),
        failure(NOW - timedelta(seconds=30), "format_failure"),
    ]
    events = [
        event(NOW - timedelta(minutes=4), "operator_enabled"),
        event(NOW - timedelta(seconds=10), "disabled"),
    ]
    c.restore(events, samples, NOW)
    assert c.disabled_until[SMALL] == 150.0
    assert list(c.windows[SMALL]) == ["format_failure"]


def test_a_hold_survives_a_gateway_restart():
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit)
    first = gateway(settings, store)
    first.operator_disable(SMALL, "maintenance")
    asyncio.run(first.save_interventions())
    second = gateway(settings, store)
    asyncio.run(second.restore())
    assert {m.name: m for m in second.controller.stats()}[SMALL].disabled_by_operator


def test_each_window_keeps_only_its_newest_samples():
    settings = Settings(_env_file=None, monitor_window=3, monitor_min_samples=3)
    c = Controller([SMALL, LARGE], settings, clock=lambda: 0.0)
    outcomes = ["call_error", "ok", "ok", "format_failure"]
    samples = [
        failure(NOW - timedelta(seconds=10 - i), o) for i, o in enumerate(outcomes)
    ]
    c.restore([], samples, NOW)
    assert list(c.windows[SMALL]) == ["ok", "ok", "format_failure"]


def test_a_store_problem_leaves_the_windows_empty(caplog):
    class Broken(MemoryStore):
        async def recent_monitor_samples(self, per_window):
            raise ConnectionError("down")

    settings = Settings(_env_file=None)
    g = gateway(settings, Broken(settings.request_store_limit))
    asyncio.run(g.restore())
    assert all(m.samples == 0 for m in g.controller.stats())
    assert "Could not restore monitor state (ConnectionError)" in caplog.text


def test_a_failed_sample_save_does_not_fail_the_request(caplog):
    class NoSamples(MemoryStore):
        async def save_monitor_samples(self, items):
            raise ConnectionError("down")

    settings = Settings(_env_file=None)
    g = gateway(settings, NoSamples(settings.request_store_limit))
    chat(g, 2)
    assert len(g.store.requests) == 2  # type: ignore[attr-defined]
    assert "Could not save monitor samples (ConnectionError)" in caplog.text
    assert g.controller.unsaved_samples == [] and g.semantic.unsaved == []


def test_a_restart_rebuilds_the_semantic_windows():
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit)
    first = gateway(settings, store)
    chat(first, 3)
    chat(first, 2, "my card was stolen")  # the large model's route
    before = first.semantic.stats()
    assert [m.samples for m in before] == [3, 2]

    second = gateway(settings, store)
    asyncio.run(second.restore())
    assert second.semantic.stats() == before


def test_a_cancelled_intervention_save_keeps_the_interventions():
    # /v1/health calls save_interventions directly, outside the shielded chat
    # save, so its client leaving can cancel the save (review of #92).
    class Slow(MemoryStore):
        async def save_interventions(self, items):
            await asyncio.sleep(10)

    settings = Settings(_env_file=None)
    g = gateway(settings, Slow(settings.request_store_limit))
    for _ in range(5):
        g.controller.record_error(SMALL, can_disable=True)
    pending = list(g.controller.unsaved)
    assert pending

    async def go() -> None:
        task = asyncio.create_task(g.save_interventions())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancel must pass on")

    asyncio.run(go())
    assert g.controller.unsaved == pending


SEMANTIC_DISABLE = (
    "intent differs from Jev's on 100% of last 10 scored replies (baseline 0%)"
)


def test_a_resumed_cooldown_keeps_its_reason():
    # A semantic disable must stay a last resort after a restart, and a
    # failure disable must not become one.
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    events = [
        Intervention(
            at=NOW - timedelta(seconds=20),
            backend=SMALL,
            action="disabled",
            reason=SEMANTIC_DISABLE,
        ),
        Intervention(
            at=NOW - timedelta(seconds=10),
            backend=LARGE,
            action="disabled",
            reason="call error rate 100% over last 5 calls",
        ),
    ]
    c.restore(events, [], NOW)
    assert c.disabled_for == {SMALL: "semantic", LARGE: "failure"}
    assert c.last_resort(SMALL)
    assert not c.last_resort(LARGE)


def test_an_expired_cooldown_leaves_no_reason():
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    old = Intervention(
        at=NOW - timedelta(minutes=5),
        backend=SMALL,
        action="disabled",
        reason=SEMANTIC_DISABLE,
    )
    c.restore([old], [], NOW)
    assert c.disabled_for == {}
    assert not c.last_resort(SMALL)


def semantic(at: datetime, outcome: str, traffic: str = "own") -> MonitorSample:
    return MonitorSample(
        at=at, model=SMALL, monitor="semantic", outcome=outcome, traffic=traffic
    )


@pytest.mark.parametrize(("actions", "own_after"), [(True, ["agree"]), (False, None)])
def test_with_actions_on_the_semantic_window_starts_after_the_re_enable(
    actions, own_after
):
    # With semantic actions on, a re-enable resets the model's own semantic
    # window, so a restart must not bring back the disagreements before it.
    settings = Settings(_env_file=None, semantic_actions_enabled=actions)
    store = MemoryStore(settings.request_store_limit)
    samples = [
        semantic(NOW - timedelta(minutes=10), "disagree"),
        semantic(NOW - timedelta(minutes=9), "disagree", "rerouted"),
        semantic(NOW - timedelta(minutes=2), "agree"),
    ]
    events = [
        event(NOW - timedelta(minutes=8), "disabled"),
        event(NOW - timedelta(minutes=7), "re-enabled"),
    ]

    async def go() -> Gateway:
        await store.save_interventions(events)
        await store.save_monitor_samples(samples)
        g = gateway(settings, store)
        await g.restore()
        return g

    g = asyncio.run(go())
    own = list(g.semantic.windows[SMALL])
    assert own == (own_after or ["disagree", "agree"])
    # Rerouted samples are never reset by a re-enable.
    assert list(g.semantic.rerouted[SMALL]) == ["disagree"]


def test_samples_from_an_ended_cooldown_do_not_come_back():
    # Review of #100: calls still running during a cooldown save samples. Live,
    # the re-enable at the cooldown's end clears them, so a restart after the
    # cooldown ended must drop them too.
    c = Controller([SMALL, LARGE], Settings(_env_file=None), clock=lambda: 0.0)
    disabled_at = NOW - timedelta(minutes=2)
    samples = [
        failure(disabled_at - timedelta(seconds=5), "format_failure"),
        failure(disabled_at + timedelta(seconds=10), "format_failure"),
        failure(disabled_at + timedelta(seconds=50), "format_failure"),
        failure(disabled_at + timedelta(seconds=61), "ok"),
    ]
    c.restore([event(disabled_at, "disabled")], samples, NOW)
    assert list(c.windows[SMALL]) == ["ok"]


def test_with_actions_on_last_resort_replies_from_a_cooldown_do_not_come_back():
    settings = Settings(_env_file=None, semantic_actions_enabled=True)
    store = MemoryStore(settings.request_store_limit)
    disabled_at = NOW - timedelta(minutes=2)
    samples = [
        semantic(disabled_at + timedelta(seconds=s), "disagree") for s in (5, 20, 40)
    ]

    async def go() -> Gateway:
        await store.save_interventions([event(disabled_at, "disabled")])
        await store.save_monitor_samples(samples)
        g = gateway(settings, store)
        await g.restore()
        return g

    g = asyncio.run(go())
    assert list(g.semantic.windows[SMALL]) == []


def test_a_cancelled_sample_save_keeps_the_samples():
    class Slow(MemoryStore):
        async def save_monitor_samples(self, items):
            await asyncio.sleep(10)

    settings = Settings(_env_file=None)
    g = gateway(settings, Slow(settings.request_store_limit))
    g.controller.record(SMALL, failed=False, can_disable=True)
    g.semantic.record(SMALL, "agree", rerouted=False)
    pending = sorted(
        g.controller.unsaved_samples + g.semantic.unsaved, key=lambda s: s.at
    )

    async def go() -> None:
        task = asyncio.create_task(g.save_interventions())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancel must pass on")

    asyncio.run(go())
    kept = g.controller.unsaved_samples + g.semantic.unsaved
    assert sorted(kept, key=lambda s: s.at) == pending


def test_sample_saves_keep_their_order():
    # Review of #100: without a lock, a slow first save could land after a
    # fast second one, and the restore would rebuild the pair inverted.
    class FirstSlow(MemoryStore):
        calls = 0

        async def save_monitor_samples(self, items):
            FirstSlow.calls += 1
            if FirstSlow.calls == 1:
                await asyncio.sleep(0.2)
            await super().save_monitor_samples(items)

    settings = Settings(_env_file=None)
    store = FirstSlow(settings.request_store_limit)
    g = gateway(settings, store)

    async def go() -> None:
        g.controller.record(SMALL, failed=True, can_disable=True)
        first = asyncio.create_task(g.save_interventions())
        await asyncio.sleep(0.05)
        g.controller.record(SMALL, failed=False, can_disable=True)
        await asyncio.gather(first, g.save_interventions())

    asyncio.run(go())
    assert [s.outcome for s in store.monitor_samples] == ["format_failure", "ok"]


def test_a_hold_older_than_the_restored_interventions_comes_back():
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit, intervention_limit=1000)
    old = NOW - timedelta(days=30)
    noise = [
        event(old + timedelta(minutes=i + 1), "kept_last_backend", LARGE)
        for i in range(600)
    ]

    async def go() -> Gateway:
        await store.save_interventions([event(old, "operator_disabled"), *noise])
        g = gateway(settings, store)
        await g.restore()
        return g

    assert asyncio.run(go()).controller.held == {SMALL}


def test_a_reset_older_than_the_newest_interventions_still_clears_the_window():
    # Audit of #100: after 600 newer interventions on the other model, the
    # bounded read missed the small model's operator enable, so failures it
    # had cleared came back.
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit, intervention_limit=1000)
    old = NOW - timedelta(days=30)
    noise = [
        event(old + timedelta(minutes=i + 1), "kept_last_backend", LARGE)
        for i in range(600)
    ]

    async def go() -> Gateway:
        await store.save_monitor_samples(
            [failure(old - timedelta(seconds=i), "format_failure") for i in (3, 2, 1)]
        )
        await store.save_interventions([event(old, "operator_enabled"), *noise])
        g = gateway(settings, store)
        await g.restore()
        return g

    assert list(asyncio.run(go()).controller.windows[SMALL]) == []


def test_a_sample_save_retried_after_a_cancel_is_stored_once():
    # Audit of #100: a cancel that lands after the store committed puts the
    # batch back, and the next save wrote the same samples again.
    class CancelAfterCommit(MemoryStore):
        slow = True

        async def save_monitor_samples(self, items):
            await super().save_monitor_samples(items)
            if CancelAfterCommit.slow:
                await asyncio.sleep(10)

    settings = Settings(_env_file=None)
    store = CancelAfterCommit(settings.request_store_limit)
    g = gateway(settings, store)
    g.controller.record(SMALL, failed=False, can_disable=True)

    async def go() -> None:
        task = asyncio.create_task(g.save_interventions())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        CancelAfterCommit.slow = False
        await g.save_interventions()

    asyncio.run(go())
    assert [s.outcome for s in store.monitor_samples] == ["ok"]


def test_a_release_change_clears_the_restored_windows():
    # A deploy or rollback clears both windows live (_release_changed), so
    # samples from the version served before it don't come back.
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit)
    deploy = ReleaseEvent(
        at=NOW - timedelta(minutes=5),
        backend=SMALL,
        action="deployed",
        version="v2-faster",
        from_version="base",
        rollback_target="base",
        actor="operator",
        reason="test",
        revision=1,
    )
    confirm = deploy.model_copy(
        update={
            "at": NOW - timedelta(minutes=1),
            "action": "confirmed",
            "from_version": None,
            "revision": 2,
        }
    )

    async def go() -> Gateway:
        await store.save_monitor_samples(
            [
                failure(NOW - timedelta(minutes=6), "format_failure"),
                semantic(NOW - timedelta(minutes=6), "disagree"),
                failure(NOW - timedelta(minutes=3), "ok"),
                semantic(NOW - timedelta(minutes=3), "agree"),
            ]
        )
        await store.save_release_events([deploy, confirm])
        g = gateway(settings, store)
        await g.restore_releases()
        await g.restore()
        return g

    g = asyncio.run(go())
    # The cut is at the deploy, not the later confirmation.
    assert list(g.controller.windows[SMALL]) == ["ok"]
    assert list(g.semantic.windows[SMALL]) == ["agree"]


def _release(at, backend, action, revision, reason="test", version="v2-faster"):
    return ReleaseEvent(
        at=at,
        backend=backend,
        action=action,
        version=version,
        from_version="base" if action == "deployed" else None,
        rollback_target="base",
        actor="operator",
        reason=reason,
        revision=revision,
    )


def test_a_release_change_behind_many_newer_release_events_still_cuts():
    # Deploy to the small model, then 300 deploy and rollback pairs on the
    # large one: more than any capped read of recent events would reach.
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit)
    large = "qwen3-4b"
    events = [_release(NOW - timedelta(minutes=5), SMALL, "deployed", 1)]
    for i in range(300):
        at = NOW - timedelta(minutes=4)
        events.append(_release(at, large, "deployed", 2 * i + 1))
        events.append(_release(at, large, "rolled_back", 2 * i + 2))

    async def go() -> Gateway:
        await store.save_monitor_samples(
            [
                failure(NOW - timedelta(minutes=6), "format_failure"),
                failure(NOW - timedelta(minutes=3), "ok"),
            ]
        )
        await store.save_release_events(events)
        g = gateway(settings, store)
        await g.restore_releases()
        await g.restore()
        return g

    g = asyncio.run(go())
    assert list(g.controller.windows[SMALL]) == ["ok"]


def test_a_kept_release_event_is_not_a_release_change_on_restart():
    # A deploy whose save could not be confirmed leaves a kept record. It
    # changes nothing while running, so it must not cut the window at restart.
    settings = Settings(_env_file=None)
    store = MemoryStore(settings.request_store_limit)
    kept = _release(
        NOW - timedelta(minutes=2),
        SMALL,
        "deployed",
        2,
        reason=f"change to v3 {KEPT_REASON_MARKER}; kept v2-faster",
    )

    async def go() -> Gateway:
        await store.save_monitor_samples(
            [
                failure(NOW - timedelta(minutes=3), "format_failure"),
                failure(NOW - timedelta(minutes=1), "format_failure"),
            ]
        )
        await store.save_release_events(
            [_release(NOW - timedelta(minutes=10), SMALL, "deployed", 1), kept]
        )
        g = gateway(settings, store)
        await g.restore_releases()
        await g.restore()
        return g

    g = asyncio.run(go())
    # Cut at the real deploy (10 min ago), not the kept record.
    assert list(g.controller.windows[SMALL]) == ["format_failure"] * 2
