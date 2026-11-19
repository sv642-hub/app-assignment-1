"""The Jev route-order experiment against a fake TypeSafe server."""

import asyncio
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app_assignment_1.banking77 import Example
from app_assignment_1.config import Settings
from app_assignment_1.labels import load_labels

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "evaluate_jev_order", ROOT / "scripts" / "evaluate_jev_order.py"
)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

LABELS = load_labels(ROOT / "config" / "labels.json")
EXAMPLES = [Example(f"secret message {i}", "card_arrival", "dev", i) for i in range(5)]
META = {"repeats": 2, "seed": 1}
Pick = Callable[[list[str]], str]


def server(pick: Pick, status: int = 200) -> httpx.MockTransport:
    """Answers the route with `pick(route options in the order sent)`."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["questions"]
        order = list(sent["route"]["criteria"])
        choice = pick(order)
        intents = list(sent["intent"]["criteria"])
        answers = {
            "route": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": {name: float(name == choice) for name in order},
            },
            "intent": {
                "type": "choice",
                "choice": intents[0],
                "confidence": 0.8,
                "probabilities": {name: float(name == intents[0]) for name in intents},
            },
        }
        usage = {"input_tokens": 3000, "output_tokens": 20}
        payload = {"model": "jev-1.14.0", "answers": answers, "usage": usage}
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def run(out: Path, pick: Pick, status: int = 200, meta: dict[str, Any] = META) -> int:
    settings = Settings(_env_file=None, jev_api_key="tsk-test")
    return asyncio.run(
        script.run_experiment(
            out, EXAMPLES, meta, settings, LABELS, 3, server(pick, status)
        )
    )


def records(out: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_a_first_position_bias_shows_as_flips(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    assert run(out, lambda order: order[0]) == 20
    summary = script.summarize(records(out))

    assert summary["valid_calls"] == 20
    assert summary["messages_with_both_orders"] == 5
    assert summary["flip_rate"][:3] == (1.0, 1.0, 1.0)
    assert summary["same_order_disagreement"][0] == 0.0
    assert summary["first_position_rate"][0] == 1.0
    assert summary["standard_rate_gap"][0] == 1.0
    assert summary["p_standard_gap"][0] == 1.0
    assert "secret" not in out.read_text()


def test_order_blind_answers_show_no_flips(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: "standard")
    summary = script.summarize(records(out))

    assert summary["flip_rate"][0] == 0.0
    assert summary["first_position_rate"][0] == 0.5
    assert summary["standard_rate_gap"][0] == 0.0


def test_both_orders_send_the_same_descriptions(tmp_path: Path) -> None:
    first = script.questions_for(LABELS, "standard_first")
    second = script.questions_for(LABELS, "complex_first")

    assert list(first["route"]["criteria"]) == ["standard", "complex"]
    assert list(second["route"]["criteria"]) == ["complex", "standard"]
    assert first["route"]["criteria"] == second["route"]["criteria"]
    assert first["intent"] == second["intent"]


def test_a_rerun_resumes_and_refuses_other_settings(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: order[0])
    saved = records(out)[1:]
    out.write_text(
        "\n".join(json.dumps(r) for r in [{"meta": META}] + saved[:-3]) + "\n"
    )

    assert run(out, lambda order: order[0]) == 3
    assert len(records(out)) == 21
    with pytest.raises(ValueError):
        run(out, lambda order: order[0], meta={**META, "repeats": 3})


def test_a_401_stops_the_run(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    with pytest.raises(script.Unauthorized):
        run(out, lambda order: order[0], status=401)
    assert len(records(out)) == 1


def test_failed_calls_are_counted_not_dropped(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: order[0], status=429)
    summary = script.summarize(records(out))

    assert summary["calls"] == 20
    assert summary["errors"] == {"http_429": 20}
    assert summary["flip_rate"] is None


def test_sample_takes_a_fixed_number_per_intent() -> None:
    examples = EXAMPLES + [Example("x", "age_limit", "dev", 9)]
    chosen = script.sample(examples, 2)
    assert sorted(e.intent for e in chosen) == [
        "age_limit",
        "card_arrival",
        "card_arrival",
    ]
    assert script.sample(examples, 2) == chosen
