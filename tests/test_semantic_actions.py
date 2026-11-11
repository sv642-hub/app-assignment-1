"""Controller actions from the semantic monitor (APP_SEMANTIC_ACTIONS_ENABLED)."""

import asyncio
import json
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
