"""Controller actions from the semantic monitor (APP_SEMANTIC_ACTIONS_ENABLED)."""

import asyncio
import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.monitor import Controller
from app_assignment_1.router import JevResult, MockJevRouter
from app_assignment_1.schemas import ChatResponse
from app_assignment_1.service import BackendUnavailable, Gateway

STANDARD = "my card has not arrived"  # card_arrival; the swap answers lost card
COMPLEX = "my card was stolen"  # lost_or_stolen_card, the large model's route
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
REASON = "intent differs from Jev's on 100% of last 3 scored replies (baseline 0%)"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_settings(
    tmp_path: Path, swaps=(1, 0), errors=(0, 0), available=(True, True), **overrides
) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend, swap, error, up in zip(
        backends, swaps, errors, available, strict=True
    ):
        backend["latency_ms"] = 0
        backend["label_swap_rate"] = swap
        backend["error_rate"] = error
        backend["available"] = up
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {
        "semantic_window": 6,
        "semantic_min_samples": 3,
        "semantic_baseline_disagreement_rate": 0.0,
        "semantic_alert_margin": 0.2,
        "semantic_actions_enabled": True,
        "controller_cooldown_seconds": 30,
    } | overrides
    return Settings(_env_file=None, backends_path=path, **values)


def gateway(settings: Settings, clock: Clock | None = None, router=None) -> Gateway:
    return Gateway(
        settings,
        router or MockJevRouter(settings.intents_path),
        clock=clock or Clock(),
    )


def send(g: Gateway, times: int = 1, message: str = STANDARD) -> list[ChatResponse]:
    async def go() -> list[ChatResponse]:
        return [await g.chat(message) for _ in range(times)]

    return asyncio.run(go())


def warm_up(g: Gateway) -> None:
    """One complex message, so the large model has answered without failing:
    the controller only disables a model while the other one is answering."""
    assert send(g, 1, COMPLEX)[0].trace.model == LARGE


def actions(g: Gateway) -> list[tuple[str, str, str]]:
    return [(i.backend, i.action, i.reason) for i in g.controller.log]


def test_off_by_default(tmp_path):
    settings = make_settings(tmp_path, semantic_actions_enabled=False)
    assert Settings(_env_file=None).semantic_actions_enabled is False
    g = gateway(settings)
    replies = send(g, 6)
    assert all(r.trace.model == SMALL for r in replies)
    assert all(r.trace.controller_action is None for r in replies)
    assert g.semantic.status(SMALL).status == "above_baseline"
    assert actions(g) == []


def test_disables_a_model_above_the_baseline(tmp_path):
    g = gateway(make_settings(tmp_path))
    warm_up(g)
    first, second, third = send(g, 3)
    assert first.trace.controller_action is None
    assert second.trace.controller_action is None
    assert third.trace.controller_action == f"disabled {SMALL}: {REASON}"
    assert actions(g) == [(SMALL, "disabled", REASON)]

    # Standard traffic moves to the large model, which agrees with Jev.
    fourth = send(g)[0]
    assert fourth.trace.model == LARGE
    assert fourth.intent == "card_arrival"
    assert "selected_backend_disabled" in (fourth.trace.fallback_reason or "")
    assert g.semantic.status(LARGE).rerouted_agreements == 1


def test_waits_for_the_minimum_samples(tmp_path):
    g = gateway(make_settings(tmp_path, semantic_min_samples=5, semantic_window=6))
    warm_up(g)
    send(g, 4)
    assert actions(g) == []
    send(g)
    assert [a[1] for a in actions(g)] == ["disabled"]


def test_reenabled_model_starts_a_fresh_semantic_window(tmp_path):
    clock = Clock()
    g = gateway(make_settings(tmp_path), clock)
    warm_up(g)
    send(g, 3)
    assert g.controller.is_disabled(SMALL)

    clock.now = 31
    reply = send(g)[0]
    assert reply.trace.model == SMALL
    assert reply.trace.controller_action is None
    status = g.semantic.status(SMALL)
    assert (status.samples, status.status) == (1, "collecting")
    assert [a[1] for a in actions(g)] == ["disabled", "re-enabled"]

    # Still swapping labels, so it is disabled again after 3 more.
    send(g, 2)
    assert [a[1] for a in actions(g)] == ["disabled", "re-enabled", "disabled"]


@pytest.mark.parametrize(("enabled", "samples_after"), [(False, 5), (True, 0)])
def test_a_failure_cooldown_resets_the_semantic_window_only_with_actions_on(
    tmp_path, enabled, samples_after
):
    # Five wrong intents, then call errors disable the model and its cooldown
    # ends. With actions off, the semantic report must survive that.
    clock = Clock()
    g = gateway(make_settings(tmp_path, semantic_actions_enabled=enabled), clock)
    send(g, 5)
    assert g.semantic.status(SMALL).status == "above_baseline"
    for _ in range(5):
        g.controller.record_error(SMALL, can_disable=True)
    assert g.controller.is_disabled(SMALL)
    clock.now = 31
    assert not g.controller.is_disabled(SMALL)
    assert g.semantic.status(SMALL).samples == samples_after


def test_never_disables_the_last_working_model(tmp_path):
    g = gateway(make_settings(tmp_path, available=(True, False)))
    replies = send(g, 6)
    assert all(r.trace.model == SMALL for r in replies)
    assert not g.controller.is_disabled(SMALL)
    # Logged once, not on every reply above the baseline.
    assert actions(g) == [(SMALL, "kept_last_backend", REASON)]


def test_kept_last_backend_is_logged_again_after_recovering(tmp_path):
    c = Controller([SMALL, LARGE], make_settings(tmp_path), Clock())
    c.record_semantic(SMALL, "high", can_disable=False)
    c.record_semantic(SMALL, "high", can_disable=False)
    c.record_semantic(SMALL, None, can_disable=False)
    c.record_semantic(SMALL, "high again", can_disable=False)
    assert [i.reason for i in c.log] == ["high", "high again"]


def test_a_disabled_model_keeps_its_cooldown(tmp_path):
    clock = Clock()
    c = Controller([SMALL, LARGE], make_settings(tmp_path), clock)
    assert c.record_semantic(SMALL, "high", can_disable=True)
    clock.now = 20
    assert c.record_semantic(SMALL, "still high", can_disable=True) is None
    assert c.disabled_until[SMALL] == 30
    assert [i.action for i in c.log] == ["disabled"]


def test_semantic_and_failure_warnings_are_separate(tmp_path):
    c = Controller([SMALL, LARGE], make_settings(tmp_path), Clock())
    c.record_semantic(SMALL, "semantic", can_disable=False)
    for _ in range(5):
        c.record_error(SMALL, can_disable=False)
    assert [i.reason[:8] for i in c.log] == ["semantic", "call err"]


def test_no_action_until_the_other_model_has_answered(tmp_path):
    # The large model has no calls yet, so there is no sign it can answer.
    g = gateway(make_settings(tmp_path))
    send(g, 4)
    assert g.semantic.status(SMALL).status == "above_baseline"
    assert actions(g) == [(SMALL, "kept_last_backend", REASON)]
    assert not g.controller.is_disabled(SMALL)
    # Once it answers, the next small reply above the baseline acts.
    warm_up(g)
    assert send(g)[0].trace.controller_action == (
        f"disabled {SMALL}: intent differs from Jev's on 100% of last 5 scored "
        "replies (baseline 0%)"
    )


def chat_or_503(g: Gateway, message: str) -> str:
    async def go() -> str:
        try:
            return (await g.chat(message)).trace.model
        except BackendUnavailable:
            return "503"

    return asyncio.run(go())


def failing_large_run(tmp_path, enabled: bool) -> tuple[int, Gateway]:
    """The review's case: the large model fails every call, the small one gives
    well-formed wrong intents. 300 requests, 1 per second, every 4th complex.
    A Health poll or Prometheus scrape each second re-enables a model as soon
    as its cooldown ends, and 61 s puts that on a standard message: the large
    model is then usable with an empty window, which is not a sign it can
    answer."""
    clock = Clock()
    settings = make_settings(
        tmp_path,
        swaps=(1, 0),
        errors=(0, 1),
        semantic_window=50,
        semantic_min_samples=10,
        semantic_alert_margin=0.15,
        semantic_actions_enabled=enabled,
        controller_cooldown_seconds=61,
    )
    g = gateway(settings, clock)
    unavailable = 0
    for i in range(300):
        clock.now = float(i)
        g.controller.stats()
        message = COMPLEX if i % 4 == 3 else STANDARD
        unavailable += chat_or_503(g, message) == "503"
    return unavailable, g


def test_never_disables_the_only_model_that_answers(tmp_path):
    off, _ = failing_large_run(tmp_path, enabled=False)
    on, g = failing_large_run(tmp_path, enabled=True)
    assert (off, on) == (0, 0)
    assert all(
        not (backend == SMALL and action == "disabled")
        for backend, action, _ in actions(g)
    )


def test_a_rerouted_reply_never_disables_the_model_that_served_it(tmp_path):
    # The large model is above the baseline, the small one answers, then one
    # call error sends a standard message to the large model.
    g = gateway(make_settings(tmp_path, swaps=(0, 0)))
    for _ in range(3):
        g.semantic.record(LARGE, "disagree", rerouted=False)
    send(g, 2)  # the small model answers, so it counts as answering
    small = g.backends[0]
    original = small.generate

    async def fail_once(message: str):
        small.generate = original
        raise ConnectionError("one-off")

    small.generate = fail_once
    reply = send(g)[0]
    assert reply.trace.model == LARGE
    assert reply.trace.fallback_reason == "selected_backend_error"
    assert reply.trace.controller_action is None
    assert not g.controller.is_disabled(LARGE)


def test_no_action_without_a_baseline(tmp_path):
    settings = make_settings(tmp_path, semantic_baseline_disagreement_rate=None)
    g = gateway(settings)
    send(g, 6)
    assert g.semantic.status(SMALL).status == "no_baseline"
    assert actions(g) == []


class FailingRouter(MockJevRouter):
    async def route(self, message: str) -> JevResult:
        raise ConnectionError("Jev is down")


def test_no_action_on_low_coverage(tmp_path):
    settings = make_settings(tmp_path)
    g = gateway(settings, router=FailingRouter(settings.intents_path))
    send(g, 6)
    assert g.semantic.status(SMALL).status == "low_coverage"
    assert actions(g) == []


def test_rerouted_disagreements_never_disable_the_other_model(tmp_path):
    # The small model is down, so the large one, which swaps labels, serves
    # standard messages. Those samples are rerouted and stay out of its window.
    settings = make_settings(
        tmp_path, swaps=(0, 1), errors=(1, 0), monitor_min_samples=20
    )
    g = gateway(settings)
    replies = send(g, 6)
    assert all(r.trace.model == LARGE for r in replies)
    assert g.semantic.status(LARGE).rerouted_disagreements == 6
    assert not g.controller.is_disabled(LARGE)
    assert all(a[0] != LARGE for a in actions(g))


def test_health_metrics_and_store_show_the_action(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/v1/chat", json={"message": COMPLEX})
        for _ in range(3):
            client.post("/v1/chat", json={"message": STANDARD})
        body = client.get("/v1/health").json()
        metrics = client.get("/metrics").text
    assert body["semantic_rules"]["actions_enabled"] is True
    assert body["status"] == "degraded"
    small = next(m for m in body["monitor"] if m["name"] == SMALL)
    assert small["disabled_by_controller"] is True
    # The format check passed every time: the semantic monitor acted alone.
    assert small["failure_rate"] == 0
    assert [
        (i["backend"], i["action"], i["reason"]) for i in body["interventions"]
    ] == [(SMALL, "disabled", REASON)]
    assert (
        'gateway_controller_interventions_total{action="disabled",model="qwen3-1.7b"}'
        " 1.0"
    ) in metrics


def outage_run(
    tmp_path, enabled: bool, outage_at: int, requests: int
) -> tuple[int, int, Gateway]:
    """The second review's cases: the small model gives well-formed wrong
    intents from the start, and the large model answers until `outage_at`,
    then fails every call. 1 request per second, every 4th complex, a Health
    poll each second. Returns the 503s and the last-resort replies."""
    clock = Clock()
    settings = make_settings(
        tmp_path,
        swaps=(1, 0),
        semantic_window=50,
        semantic_min_samples=10,
        semantic_alert_margin=0.15,
        semantic_actions_enabled=enabled,
        controller_cooldown_seconds=60,
    )
    g = gateway(settings, clock)
    unavailable = last_resort = 0
    for i in range(requests):
        clock.now = float(i)
        if i == outage_at:
            g.backends[1].config.error_rate = 1
        g.controller.stats()
        message = COMPLEX if i % 4 == 3 else STANDARD
        try:
            reply = asyncio.run(g.chat(message))
        except BackendUnavailable:
            unavailable += 1
            continue
        last_resort += "semantic_last_resort" in (reply.trace.fallback_reason or "")
    return unavailable, last_resort, g


@pytest.mark.parametrize(("outage_at", "requests"), [(13, 80), (60, 300)])
def test_a_later_outage_of_the_other_model_never_gives_503s(
    tmp_path, outage_at, requests
):
    # Before the fix: 59 of 80 and 12 of 300 were 503s with actions on.
    off, off_last_resort, _ = outage_run(tmp_path, False, outage_at, requests)
    on, on_last_resort, g = outage_run(tmp_path, True, outage_at, requests)
    assert (off, on) == (0, 0)
    assert off_last_resort == 0
    # The small model was disabled for its intents, then served as a last
    # resort while the large one failed.
    assert (SMALL, "disabled") in {(b, a) for b, a, _ in actions(g)}
    assert on_last_resort > 0


def test_an_outage_just_before_the_disable_never_gives_503s(tmp_path):
    # The large model answers 4 complex messages, then fails 3: 3 failures in
    # 7 is below the threshold, so it still counts as answering, and the next
    # standard replies disable the small model.
    g = gateway(make_settings(tmp_path))
    send(g, 4, COMPLEX)
    g.backends[1].config.error_rate = 1
    assert [r.trace.model for r in send(g, 3, COMPLEX)] == [SMALL] * 3
    assert g.controller.answering(LARGE)
    first = send(g, 3)
    assert first[-1].trace.controller_action == f"disabled {SMALL}: {REASON}"
    # Before the fix, these three were 503s.
    later = send(g, 3)
    assert [r.trace.model for r in later] == [SMALL] * 3
    assert all(
        r.trace.fallback_reason == "selected_backend_disabled, semantic_last_resort"
        for r in later
    )
    # The large model failed each time, but it is kept: the small model is out.
    assert not g.controller.is_disabled(LARGE)
    assert g.controller.is_disabled(SMALL)


def test_only_a_semantic_disable_is_a_last_resort(tmp_path):
    clock = Clock()
    c = Controller([SMALL, LARGE], make_settings(tmp_path), clock)
    assert not c.last_resort(SMALL)  # not disabled
    c.record_semantic(SMALL, "wrong intents", can_disable=True)
    assert c.last_resort(SMALL)
    assert c.stats()[0].disabled_for == "semantic"
    for _ in range(5):
        c.record_error(LARGE, can_disable=True)
    assert c.is_disabled(LARGE)
    assert not c.last_resort(LARGE)  # disabled for failures
    assert c.stats()[1].disabled_for == "failure"
    # Its failing window already rules it out; the reason rules it out too.
    c.windows[LARGE].clear()
    assert not c.last_resort(LARGE)
    # Failing some of its own calls does not take it out: it is the last model
    # that can answer, and excluding it would freeze its window.
    for _ in range(5):
        c.record_error(SMALL, can_disable=False)
    assert c.last_resort(SMALL)
    clock.now = 31
    stats = c.stats()  # re-enables both
    assert [m.disabled_for for m in stats] == [None, None]
    assert c.disabled_for == {}


def test_health_reports_answering(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        before = client.get("/v1/health").json()
        client.post("/v1/chat", json={"message": COMPLEX})
        after = client.get("/v1/health").json()
    answering = {m["name"]: m["answering"] for m in before["monitor"]}
    assert answering == {SMALL: False, LARGE: False}
    answering = {m["name"]: m["answering"] for m in after["monitor"]}
    assert answering == {SMALL: False, LARGE: True}


def flaky_small_run(tmp_path, enabled: bool) -> tuple[int, list[int], Gateway]:
    """The third review's case: the small model gives wrong intents and fails
    some of its calls, and the large model fails every call from request 40.
    200 requests 0.25 s apart, every 2nd complex, cooldown 61 s, seeded mocks.
    The review used 30% errors with its own seeds; with these seeds, 45% is
    what pushes the small model's window over the threshold. Returns the 503s
    and the small model's calls per request after the outage."""
    clock = Clock()
    settings = make_settings(
        tmp_path,
        swaps=(1, 0),
        errors=(0.45, 0),
        semantic_window=50,
        semantic_min_samples=10,
        semantic_alert_margin=0.15,
        semantic_actions_enabled=enabled,
        controller_cooldown_seconds=61,
    )
    g = gateway(settings, clock)
    for seed, backend in enumerate(g.backends):
        backend.rng = random.Random(seed)
    unavailable, small_calls = 0, []
    for i in range(200):
        clock.now = i * 0.25
        if i == 40:
            g.backends[1].config.error_rate = 1
        g.controller.stats()
        before = g.usage[SMALL].attempted_calls
        message = COMPLEX if i % 2 else STANDARD
        unavailable += chat_or_503(g, message) == "503"
        if i >= 40:
            small_calls.append(g.usage[SMALL].attempted_calls - before)
    return unavailable, small_calls, g


def test_a_flaky_last_resort_keeps_answering(tmp_path):
    # Before the fix, the last resort was dropped once its own failure rate
    # reached the threshold, and every later request was a 503 (117 of 200
    # in the review, against 39 with actions off; 132 against 67 here).
    off, off_calls, _ = flaky_small_run(tmp_path, enabled=False)
    on, on_calls, g = flaky_small_run(tmp_path, enabled=True)
    assert (SMALL, "disabled") in {(b, a) for b, a, _ in actions(g)}
    # After the outage, every request still calls the small model once,
    # whether it is enabled or a last resort.
    assert set(off_calls) == {1}
    assert set(on_calls) == {1}
    # So the 503s come only from the small model's own errors.
    assert on <= off + 10


def test_a_cooldown_that_ends_during_another_call_is_used(tmp_path):
    # The small model is disabled until t = 30. A request at t = 29 skips it,
    # and the large model fails at t = 30.5, after the cooldown ended.
    clock = Clock()
    g = gateway(make_settings(tmp_path, swaps=(0, 0)), clock)
    g.controller.record_semantic(SMALL, "wrong intents", can_disable=True)
    large = g.backends[1]

    async def slow_failure(message: str):
        clock.now = 30.5
        raise TimeoutError("slow")

    large.generate = slow_failure
    clock.now = 29
    reply = send(g)[0]
    assert reply.trace.model == SMALL
    # Re-enabled, not a last resort.
    assert reply.trace.fallback_reason == "selected_backend_disabled"
    assert not g.controller.is_disabled(SMALL)


def test_the_reserve_is_kept_for_a_last_resort(tmp_path):
    # The small model is disabled for its intents. A complex message goes to
    # the large model first, which hangs. Because the small model can still
    # answer as a last resort, the large model gets the deadline minus the
    # reserve, and the small model answers in the time left.
    settings = make_settings(
        tmp_path,
        swaps=(0, 0),
        request_deadline_seconds=1.0,
        alternate_reserve_seconds=0.5,
        jev_timeout_seconds=0.2,
    )
    g = gateway(settings)
    g.controller.record_semantic(SMALL, "wrong intents", can_disable=True)

    async def hang(message: str):
        await asyncio.sleep(10)

    g.backends[1].generate = hang
    reply = send(g, 1, COMPLEX)[0]
    assert reply.trace.model == SMALL
    assert reply.trace.fallback_reason == "selected_backend_error, semantic_last_resort"
    assert g.usage[LARGE].failed_calls == 1
    assert reply.trace.latency_ms < 1000


def test_a_held_model_is_never_a_last_resort(tmp_path):
    # The small model is disabled for its intents, then an operator holds it.
    # When the large model fails, the held model must not answer.
    g = gateway(make_settings(tmp_path))
    warm_up(g)
    send(g, 3)
    assert g.controller.last_resort(SMALL)
    g.operator_disable(SMALL, "maintenance")
    assert not g.controller.last_resort(SMALL)
    g.backends[1].config.error_rate = 1
    with pytest.raises(BackendUnavailable):
        send(g)
    assert g.controller.is_disabled(SMALL)


def test_an_operator_enable_clears_the_reason_and_the_semantic_window(tmp_path):
    g = gateway(make_settings(tmp_path))
    warm_up(g)
    send(g, 3)
    assert g.controller.disabled_for == {SMALL: "semantic"}
    assert g.semantic.status(SMALL).samples == 3
    g.operator_enable(SMALL, "checked")
    assert g.controller.disabled_for == {}
    assert g.semantic.status(SMALL).samples == 0
    assert not g.controller.is_disabled(SMALL)
    assert [a[1] for a in actions(g)] == ["disabled", "operator_enabled"]


class RealJev(MockJevRouter):
    """The mock's answers, reported as real Jev."""

    mocked = False

    async def route(self, message: str) -> JevResult:
        result = await super().route(message)
        return result.model_copy(update={"mocked": False})


def test_a_mock_model_is_never_disabled_against_real_jev(tmp_path):
    # Real Jev and mock models disagree by design, so the replies are not
    # compared, and the controller never acts on them.
    settings = make_settings(tmp_path)
    g = gateway(settings, router=RealJev(settings.intents_path))
    warm_up(g)
    replies = send(g, 6)
    assert all(r.trace.model == SMALL for r in replies)
    assert actions(g) == []
    assert g.semantic.not_compared[SMALL] == 6
