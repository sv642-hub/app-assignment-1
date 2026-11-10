"""Semantic monitor: scoring against Jev's intent, windows, status and wiring."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.backends import MockBackend
from app_assignment_1.config import Settings
from app_assignment_1.intents import load_intents
from app_assignment_1.records import JevAssessment
from app_assignment_1.router import JevResult, MockJevRouter
from app_assignment_1.schemas import BackendConfig, PolicyCard
from app_assignment_1.semantic import SemanticMonitor, score
from app_assignment_1.service import check_reply

STANDARD = "my card has not arrived"  # card_arrival, standard route
COMPLEX = "my card was stolen"  # lost_or_stolen_card, complex route
NO_MATCH = "what is the weather like"
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"


def make_settings(tmp_path: Path, swaps=(0, 0), errors=(0, 0), **overrides):
    backends = json.loads(Path("config/backends.json").read_text())
    for backend, swap, error in zip(backends, swaps, errors, strict=True):
        backend["latency_ms"] = 0
        backend["label_swap_rate"] = swap
        backend["error_rate"] = error
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {
        "semantic_window": 6,
        "semantic_min_samples": 3,
        "semantic_min_coverage": 0.5,
        "semantic_baseline_disagreement_rate": 0.0,
        "semantic_alert_margin": 0.2,
    } | overrides
    return Settings(_env_file=None, backends_path=path, **values)


def intent_record(status="ok", choice="card_arrival") -> JevAssessment:
    call = {
        "request_id": uuid4(),
        "call_id": uuid4(),
        "question": "intent",
        "finished_at": datetime.now(UTC),
        "mocked": True,
        "option_set_version": "mock-intents-test",
    }
    if status == "ok":
        return JevAssessment(
            **call,
            status="ok",
            latency_ms=1,
            jev_model="mock-jev",
            input_tokens=0,
            output_tokens=0,
            choice=choice,
            confidence=0.9,
            probabilities={choice: 0.9, "other_label": 0.1},
        )
    if status == "skipped":
        return JevAssessment(**call, status="skipped", skip_reason="circuit_open")
    return JevAssessment(**call, status=status, error_type=status)


def health(client: TestClient, name: str = SMALL) -> dict:
    body = client.get("/v1/health").json()
    return next(item for item in body["semantic"] if item["name"] == name)


# Scoring one reply


def test_same_label_agrees_and_different_label_disagrees():
    assert score("card_arrival", [intent_record()]) == "agree"
    assert score("exchange_rate", [intent_record()]) == "disagree"


def test_unknown_is_a_label_like_any_other():
    assert score("unknown", [intent_record(choice="unknown")]) == "agree"
    assert score("card_arrival", [intent_record(choice="unknown")]) == "disagree"


@pytest.mark.parametrize("status", ["timeout", "error", "skipped"])
def test_no_jev_answer_is_no_signal(status):
    assert score("card_arrival", [intent_record(status)]) == "no_signal"


def test_missing_records_are_no_signal():
    # The gateway logs and drops records it cannot build.
    assert score("card_arrival", []) == "no_signal"


# Windows and status


def monitor(tmp_path, **overrides) -> SemanticMonitor:
    return SemanticMonitor([SMALL, LARGE], make_settings(tmp_path, **overrides))


def status(m: SemanticMonitor, name: str = SMALL):
    return next(item for item in m.stats() if item.name == name)


def test_empty_window_has_no_rate(tmp_path):
    s = status(monitor(tmp_path))
    assert (s.disagreement_rate, s.coverage, s.status) == (None, None, "collecting")


def test_collecting_until_minimum_scored_samples(tmp_path):
    m = monitor(tmp_path)
    m.record(SMALL, "disagree", rerouted=False)
    m.record(SMALL, "disagree", rerouted=False)
    assert status(m).status == "collecting"
    m.record(SMALL, "disagree", rerouted=False)
    assert status(m).status == "above_baseline"


def test_normal_within_margin_and_flagged_above_it(tmp_path):
    m = monitor(tmp_path)  # baseline 0, margin 0.2, window 6
    for sample in ["agree"] * 5 + ["disagree"]:
        m.record(SMALL, sample, rerouted=False)
    s = status(m)
    assert (s.disagreement_rate, s.status) == (0.167, "normal")
    m.record(SMALL, "disagree", rerouted=False)  # window: 4 agree, 2 disagree
    s = status(m)
    assert (s.samples, s.disagreement_rate, s.status) == (6, 0.333, "above_baseline")


def test_rate_equal_to_baseline_plus_margin_is_normal(tmp_path):
    m = monitor(tmp_path, semantic_alert_margin=0.25)
    for sample in ["agree"] * 3 + ["disagree"]:
        m.record(SMALL, sample, rerouted=False)
    assert (status(m).disagreement_rate, status(m).status) == (0.25, "normal")


def test_no_baseline_reports_rates_without_flagging(tmp_path):
    m = monitor(tmp_path, semantic_baseline_disagreement_rate=None)
    for _ in range(3):
        m.record(SMALL, "disagree", rerouted=False)
    s = status(m)
    assert (s.disagreement_rate, s.status) == (1.0, "no_baseline")


def test_low_coverage_wins_over_a_healthy_looking_rate(tmp_path):
    # A Jev outage must not look like a healthy model.
    m = monitor(tmp_path)
    for sample in ["no_signal"] * 4 + ["agree"] * 2:
        m.record(SMALL, sample, rerouted=False)
    s = status(m)
    assert (s.coverage, s.disagreement_rate, s.status) == (0.333, 0.0, "low_coverage")


def test_low_coverage_also_hides_disagreement(tmp_path):
    m = monitor(tmp_path)
    for sample in ["no_signal"] * 3 + ["disagree"]:
        m.record(SMALL, sample, rerouted=False)
    assert status(m).status == "low_coverage"


def test_window_slides(tmp_path):
    m = monitor(tmp_path)
    for _ in range(6):
        m.record(SMALL, "disagree", rerouted=False)
    for _ in range(6):
        m.record(SMALL, "agree", rerouted=False)
    s = status(m)
    assert (s.samples, s.disagreements, s.status) == (6, 0, "normal")


def test_rerouted_samples_stay_out_of_the_window(tmp_path):
    m = monitor(tmp_path)
    for _ in range(4):
        m.record(LARGE, "disagree", rerouted=True)
    s = status(m, LARGE)
    assert (s.samples, s.status) == (0, "collecting")
    assert (s.rerouted_agreements, s.rerouted_disagreements) == (0, 4)


def test_models_have_separate_windows(tmp_path):
    m = monitor(tmp_path)
    for _ in range(3):
        m.record(SMALL, "disagree", rerouted=False)
    assert status(m, SMALL).status == "above_baseline"
    assert status(m, LARGE).samples == 0


def test_min_samples_cannot_exceed_window(tmp_path):
    with pytest.raises(ValidationError, match="semantic_min_samples"):
        make_settings(tmp_path, semantic_window=3, semantic_min_samples=4)


# The label-swap fault


def mock_backend(swap: float) -> MockBackend:
    intents = load_intents(Path("config/intents.json"))
    cards = {
        name: PolicyCard.model_validate(card)
        for name, card in json.loads(
            Path("config/policy_cards.json").read_text()
        ).items()
    }
    config = BackendConfig(
        name=SMALL, route="standard", latency_ms=0, label_swap_rate=swap
    )
    return MockBackend(config, intents, cards)


def test_label_swap_gives_another_carded_intent_that_passes_the_format_check():
    backend = mock_backend(1)
    reply = asyncio.run(backend.generate(STANDARD))
    assert reply.intent == "lost_or_stolen_card"
    assert reply.intent in backend.cards
    assert reply.policy_facts == backend.cards["lost_or_stolen_card"].facts
    assert check_reply(reply.reply) == []


def test_label_swap_wraps_from_the_last_intent_to_the_first():
    backend = mock_backend(1)
    reply = asyncio.run(backend.generate("the atm gave me less cash"))
    assert reply.intent == backend.intents[0].intent


def test_label_swap_leaves_unmatched_messages_alone():
    reply = asyncio.run(mock_backend(1).generate(NO_MATCH))
    assert reply.intent == "unknown"


def test_no_label_swap_by_default():
    reply = asyncio.run(mock_backend(0).generate(STANDARD))
    assert reply.intent == "card_arrival"


def test_label_swap_rate_is_a_share():
    with pytest.raises(ValidationError):
        BackendConfig(name=SMALL, route="standard", latency_ms=0, label_swap_rate=1.5)


# Through the gateway


def test_healthy_mocks_agree_with_jev(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        for message in (STANDARD, STANDARD, NO_MATCH):
            assert client.post("/v1/chat", json={"message": message}).is_success
        s = health(client)
    assert (s["agreements"], s["disagreements"], s["status"]) == (3, 0, "normal")


def test_label_swap_is_invisible_to_the_format_check_but_not_to_the_monitor(
    tmp_path,
):
    with TestClient(create_app(make_settings(tmp_path, swaps=(1, 0)))) as client:
        for _ in range(3):
            trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
            assert trace["format_check"] == "passed"
            assert trace["controller_action"] is None
        body = client.get("/v1/health").json()
    small = next(item for item in body["semantic"] if item["name"] == SMALL)
    assert (small["disagreements"], small["status"]) == (3, "above_baseline")
    assert all(item["failure_rate"] in (0, None) for item in body["monitor"])
    assert body["status"] == "ok"


def test_the_other_model_is_unaffected(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, swaps=(1, 0)))) as client:
        for _ in range(3):
            client.post("/v1/chat", json={"message": COMPLEX})
        assert health(client, LARGE)["status"] == "normal"
        assert health(client, SMALL)["samples"] == 0


class FailingRouter(MockJevRouter):
    async def route(self, message: str) -> JevResult:
        raise ConnectionError("Jev is down")


def test_jev_failure_lowers_coverage_instead_of_looking_healthy(tmp_path):
    settings = make_settings(tmp_path)
    router = FailingRouter(settings.intents_path)
    with TestClient(create_app(settings, router=router)) as client:
        for _ in range(3):
            client.post("/v1/chat", json={"message": STANDARD})
        s = health(client)
    assert (s["no_signal"], s["coverage"], s["status"]) == (3, 0.0, "low_coverage")
    assert s["disagreement_rate"] is None


def test_open_breaker_is_no_signal(tmp_path):
    settings = make_settings(
        tmp_path, jev_breaker_failures=1, jev_breaker_cooldown_seconds=600
    )
    router = FailingRouter(settings.intents_path)
    with TestClient(create_app(settings, router=router)) as client:
        client.post("/v1/chat", json={"message": STANDARD})  # opens the breaker
        trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
        assert trace["fallback_reason"] == "jev_circuit_open"
        assert health(client)["no_signal"] == 2


def test_failed_format_check_gives_no_semantic_sample(tmp_path):
    backends = json.loads(Path("config/backends.chaos.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "chaos.json"
    path.write_text(json.dumps(backends))
    settings = Settings(_env_file=None, backends_path=path, monitor_min_samples=5)
    with TestClient(create_app(settings)) as client:
        trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
        assert trace["format_check"] == "failed"
        assert health(client)["samples"] == 0


def test_rerouted_traffic_is_kept_apart(tmp_path):
    # The small model is down, so the large model serves standard messages.
    settings = make_settings(tmp_path, errors=(1, 0), monitor_min_samples=20)
    with TestClient(create_app(settings)) as client:
        for _ in range(3):
            trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
            assert trace["model"] == LARGE
        s = health(client, LARGE)
    assert (s["samples"], s["rerouted_agreements"]) == (0, 3)


def test_health_reports_the_rules(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        rules = client.get("/v1/health").json()["semantic_rules"]
    assert rules == {
        "window": 6,
        "min_samples": 3,
        "min_coverage": 0.5,
        "baseline_disagreement_rate": 0.0,
        "alert_margin": 0.2,
    }


def test_default_settings_have_no_baseline():
    settings = Settings(_env_file=None)
    assert settings.semantic_baseline_disagreement_rate is None


# Metrics


def metric_lines(text: str, name: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(name)]


def test_metrics_count_samples_and_flag_the_model(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, swaps=(1, 0)))) as client:
        for _ in range(3):
            client.post("/v1/chat", json={"message": STANDARD})
        text = client.get("/metrics").text
    assert (
        'gateway_semantic_samples_total{model="qwen3-1.7b",sample="disagree",'
        'traffic="own"} 3.0'
    ) in text
    assert 'gateway_semantic_disagreement_rate{model="qwen3-1.7b"} 1.0' in text
    assert 'gateway_semantic_above_baseline{model="qwen3-1.7b"} 1.0' in text
    assert 'gateway_semantic_above_baseline{model="qwen3-4b"} 0.0' in text


def test_empty_window_has_no_rate_metric(tmp_path):
    # An empty window must not show up as a 0% disagreement rate.
    with TestClient(create_app(make_settings(tmp_path))) as client:
        text = client.get("/metrics").text
    assert metric_lines(text, "gateway_semantic_disagreement_rate{") == []
    assert metric_lines(text, "gateway_semantic_coverage{") == []
    assert len(metric_lines(text, "gateway_semantic_samples_total{")) == 12


# The demo override


def test_labelswap_override_points_at_a_valid_config():
    override = yaml.safe_load(Path("compose.labelswap.yml").read_text())
    env = override["services"]["gateway"]["environment"]
    configs = [
        BackendConfig.model_validate(item)
        for item in json.loads(Path(env["APP_BACKENDS_PATH"]).read_text())
    ]
    swaps = {config.name: config.label_swap_rate for config in configs}
    assert swaps == {SMALL: 1, LARGE: 0}
    settings = Settings(
        _env_file=None,
        backends_path=Path(env["APP_BACKENDS_PATH"]),
        semantic_baseline_disagreement_rate=env[
            "APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE"
        ],
    )
    assert settings.semantic_baseline_disagreement_rate == 0
