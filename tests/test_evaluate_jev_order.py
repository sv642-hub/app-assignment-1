"""The Jev route-order experiment against a fake TypeSafe server."""

import asyncio
import importlib.util
import json
import random
from collections import Counter
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
META = {
    "messages": 5,
    "repeats": 2,
    "seed": 1,
    "max_calls": 1000,
    "max_errors_in_a_row": 1000,
}
Pick = Callable[[list[str]], str]
Fail = Callable[[str, list[str]], int | None]


def server(pick: Pick, fail: Fail = lambda text, order: None) -> httpx.MockTransport:
    """Answers the route with `pick(route options in the order sent)`, or with
    the status `fail(message, order)` returns."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent = body["questions"]
        order = list(sent["route"]["criteria"])
        status = fail(body["state"], order)
        if status is not None:
            return httpx.Response(status, json={})
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
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def always(status: int) -> Fail:
    return lambda text, order: status


def run(
    out: Path,
    pick: Pick,
    fail: Fail = lambda text, order: None,
    meta: dict[str, Any] = META,
    concurrency: int = 3,
    examples: list[Example] = EXAMPLES,
) -> int:
    settings = Settings(_env_file=None, jev_api_key="tsk-test")
    return asyncio.run(
        script.run_experiment(
            out, examples, meta, settings, LABELS, concurrency, server(pick, fail)
        )
    )


def lines(out: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.read_text().splitlines()]


def records(out: Path) -> list[dict[str, Any]]:
    """The settings line and the answers, without the "start" lines."""
    return [r for r in lines(out) if "start" not in r]


def call(row: int, order: str, repeat: int, route: str | None, **extra: Any) -> dict:
    record = {"row": row, "order": order, "repeat": repeat, "latency_ms": 500}
    if route is None:
        return record | {"error": "timeout"} | extra
    p_standard = float(route == "standard")
    return (
        record
        | {
            "error": None,
            "route": route,
            "p_standard": p_standard,
            "input_tokens": 3000,
            "output_tokens": 20,
        }
        | extra
    )


def first_listed(order: str) -> str:
    return script.ORDERS[order][0]


def test_a_first_position_bias_shows_as_flips(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    assert run(out, lambda order: order[0]) == 20
    summary = script.summarize(records(out))

    assert summary["answered_calls"] == 20
    assert summary["messages_answered_in_both_orders"] == 5
    assert summary["flip_rate"] == {"mean": 1.0, "low": 1.0, "high": 1.0, "n": 5}
    assert summary["same_order_disagreement"]["mean"] == 0.0
    assert summary["flip_minus_same_order"]["mean"] == 1.0
    assert summary["first_position_rate"]["mean"] == 1.0
    assert summary["standard_rate_gap"]["mean"] == 1.0
    assert summary["p_standard_gap"]["mean"] == 1.0
    assert "secret" not in out.read_text()
    for record in records(out)[1:]:
        assert tuple(record["route_order"]) == script.ORDERS[record["order"]]


def test_order_blind_answers_show_no_flips(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: "standard")
    summary = script.summarize(records(out))

    assert summary["flip_rate"]["mean"] == 0.0
    assert summary["first_position_rate"]["mean"] == 0.5
    assert summary["standard_rate_gap"]["mean"] == 0.0


def test_calls_lost_in_one_order_do_not_fake_a_first_position_bias(
    tmp_path: Path,
) -> None:
    # Review on #90: an order-blind model whose first standard_first call per
    # message got a 429 used to show a first-position rate of 0.667.
    seen: Counter[tuple[str, str]] = Counter()

    def first_standard_first_call_fails(text: str, order: list[str]) -> int | None:
        seen[text, order[0]] += 1
        return 429 if order[0] == "standard" and seen[text, order[0]] == 1 else None

    out = tmp_path / "calls.jsonl"
    run(out, lambda order: "complex", first_standard_first_call_fails)
    summary = script.summarize(records(out))

    assert summary["calls_by_order"]["standard_first"]["answered"] == 5
    assert summary["calls_by_order"]["standard_first"]["failed_attempts"] == {
        "http_429": 5
    }
    assert summary["calls_by_order"]["complex_first"]["answered"] == 10
    assert summary["first_position_rate"]["mean"] == 0.5
    assert summary["flip_rate"]["mean"] == 0.0


def test_asymmetric_timeouts_do_not_fake_a_bias() -> None:
    # Audit on #90: four timeouts in one order made a router that always says
    # standard look 75% first-position.
    calls = []
    for row in range(4):
        calls += [call(row, "standard_first", 0, "standard")]
        calls += [call(row, "standard_first", 1, None)]
        calls += [call(row, "complex_first", k, "standard") for k in range(2)]
    summary = script.summarize([{"meta": {**META, "messages": 4}}, *calls])

    assert summary["first_position_rate"]["mean"] == 0.5
    assert summary["flip_rate"]["mean"] == 0.0
    assert summary["standard_rate_gap"]["mean"] == 0.0
    assert summary["calls_by_order"]["standard_first"]["failed_attempts"] == {
        "timeout": 4
    }


def test_flips_pair_every_cross_order_call() -> None:
    # Review on #90: only standard_first repeat 1 and complex_first repeat 0
    # answered, so pairing by repeat number found no pairs.
    calls = []
    for row in range(5):
        calls += [call(row, "standard_first", 1, first_listed("standard_first"))]
        calls += [call(row, "complex_first", 0, first_listed("complex_first"))]
    summary = script.summarize([{"meta": META}, *calls])

    assert summary["messages_answered_in_both_orders"] == 5
    assert summary["flip_rate"] == {"mean": 1.0, "low": 1.0, "high": 1.0, "n": 5}


def test_messages_answered_in_one_order_are_reported_not_used() -> None:
    calls = [call(0, "standard_first", 0, "standard")]
    calls += [call(1, order, 0, "standard") for order in script.ORDERS]
    summary = script.summarize([{"meta": {**META, "messages": 2}}, *calls])

    assert summary["messages_answered_in_one_order_only"] == 1
    assert summary["messages_answered_in_both_orders"] == 1
    assert summary["paired_coverage"] == 0.5
    assert summary["flip_rate"]["n"] == 1


def test_a_small_sample_is_inconclusive(tmp_path: Path) -> None:
    # Review on #90: 5 messages and no flips gave a zero-width interval.
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: order[0])
    summary = script.summarize(records(out))

    assert summary["verdict"].startswith("inconclusive")


def test_the_verdict_needs_flips_beyond_same_order_noise() -> None:
    n = script.MIN_MESSAGES
    meta = {**META, "messages": n}

    def calls(route_for: Callable[[int, str, int], str]) -> list[dict[str, Any]]:
        return [{"meta": meta}] + [
            call(row, order, k, route_for(row, order, k))
            for row in range(n)
            for order in script.ORDERS
            for k in range(2)
        ]

    biased = script.summarize(calls(lambda row, order, k: first_listed(order)))
    assert biased["verdict"] == "flips exceed same-order disagreement"

    # Noisy but order-blind: every call is a coin flip, so two calls in the
    # same order disagree as often as two calls in opposite orders.
    rng = random.Random(0)
    noisy = script.summarize(
        calls(lambda row, order, k: rng.choice(["standard", "complex"]))
    )
    assert 0.3 < noisy["flip_rate"]["mean"] < 0.7
    assert noisy["verdict"] == "inconclusive: the interval covers 0"

    # Below the paired coverage, even a clear effect is inconclusive.
    sparse = script.summarize(
        [{"meta": {**meta, "messages": 2 * n}}]
        + calls(lambda row, order, k: first_listed(order))[1:]
    )
    assert sparse["verdict"].startswith("inconclusive: too few")


def test_both_orders_send_the_same_descriptions() -> None:
    first = script.questions_for(LABELS, "standard_first")
    second = script.questions_for(LABELS, "complex_first")

    assert list(first["route"]["criteria"]) == ["standard", "complex"]
    assert list(second["route"]["criteria"]) == ["complex", "standard"]
    assert first["route"]["criteria"] == second["route"]["criteria"]
    assert first["intent"] == second["intent"]


def test_the_questions_hash_pins_the_route_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = script.questions_sha256(LABELS)
    assert before["standard_first"] != before["complex_first"]
    monkeypatch.setitem(script.ROUTE_CRITERIA, "standard", "Something else.")
    assert script.questions_sha256(LABELS) != before


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
    with pytest.raises(ValueError):
        run(out, lambda order: order[0], meta={**META, "timeout_seconds": 0.001})


def test_failed_calls_are_retried_on_a_rerun(tmp_path: Path) -> None:
    # Review on #90: a 429 burst used to write off every call it hit.
    out = tmp_path / "calls.jsonl"
    assert run(out, lambda order: order[0], always(429)) == 20
    summary = script.summarize(records(out))
    assert summary["calls_by_order"]["standard_first"]["failed_attempts"] == {
        "http_429": 10
    }
    assert summary["flip_rate"] is None
    assert summary["verdict"].startswith("inconclusive")

    assert run(out, lambda order: order[0]) == 20
    assert run(out, lambda order: order[0]) == 0
    summary = script.summarize(records(out))
    assert summary["attempts"] == 40
    assert summary["answered_calls"] == 20
    assert summary["flip_rate"]["mean"] == 1.0


def started(out: Path) -> int:
    return sum("start" in r for r in lines(out))


def test_the_call_budget_holds_across_reruns(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    meta = {**META, "max_calls": 7}
    with pytest.raises(script.Stopped, match="budget"):
        run(out, lambda order: order[0], meta=meta)
    assert started(out) == 7
    with pytest.raises(script.Stopped, match="budget"):
        run(out, lambda order: order[0], meta=meta)
    assert started(out) == 7


def test_a_spent_budget_can_be_raised_on_a_rerun(tmp_path: Path) -> None:
    # Review on #90: raising --max-calls used to need a new --out.
    out = tmp_path / "calls.jsonl"
    with pytest.raises(script.Stopped, match="budget"):
        run(out, lambda order: order[0], meta={**META, "max_calls": 18})
    assert run(out, lambda order: order[0], meta={**META, "max_calls": 22}) == 2
    summary = script.summarize(records(out))
    assert summary["answered_calls"] == 20
    assert started(out) == 20


def test_calls_sent_but_not_saved_still_count(tmp_path: Path) -> None:
    # Review on #90: a Ctrl-C with calls in flight left them uncharged.
    out = tmp_path / "calls.jsonl"
    out.write_text(
        json.dumps({"meta": META})
        + "\n"
        + "".join(
            json.dumps(
                {"start": True, "row": r, "order": "standard_first", "repeat": 0}
            )
            + "\n"
            for r in range(5)
        )
    )
    with pytest.raises(script.Stopped, match="budget"):
        run(out, lambda order: order[0], meta={**META, "max_calls": 5})
    assert started(out) == 5


def test_a_last_line_without_a_newline_does_not_break_the_file(
    tmp_path: Path,
) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: order[0])
    saved = out.read_text().rstrip("\n").splitlines()
    out.write_text("\n".join(saved[:-2]))  # complete JSON, no newline
    assert run(out, lambda order: order[0]) >= 1
    assert script.summarize(records(out))["answered_calls"] == 20


def test_repeats_must_be_at_least_two(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repeats"):
        run(
            tmp_path / "calls.jsonl",
            lambda order: order[0],
            meta={**META, "repeats": 1},
        )


def test_a_run_stops_after_errors_in_a_row(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    meta = {**META, "max_errors_in_a_row": 3}
    with pytest.raises(script.Stopped, match="3 failed calls in a row"):
        run(out, lambda order: order[0], always(503), meta=meta, concurrency=1)
    assert len(records(out)) == 1 + 3


def test_a_401_stops_the_run(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    with pytest.raises(script.Stopped, match="401"):
        run(out, lambda order: order[0], always(401))
    assert len(records(out)) == 1


def test_concurrency_must_be_at_least_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run(tmp_path / "calls.jsonl", lambda order: order[0], concurrency=0)


def test_a_cut_off_last_line_is_dropped_and_redone(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    run(out, lambda order: order[0])
    lines = out.read_text().splitlines()
    out.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:40])

    assert script.summarize(script.read_records(out))["answered_calls"] == 19
    assert run(out, lambda order: order[0]) == 1
    assert script.summarize(records(out))["answered_calls"] == 20


def test_an_empty_out_file_starts_a_new_run(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    out.touch()
    assert run(out, lambda order: order[0]) == 20


def test_latency_includes_failed_calls_and_uses_one_rule() -> None:
    calls = [call(0, "standard_first", k, "standard") for k in range(8)]
    calls += [call(0, "complex_first", k, None, latency_ms=15000) for k in range(2)]
    summary = script.summarize([{"meta": META}, *calls])

    assert summary["latency_ms_answered"] == {"p50": 500, "p95": 500}
    assert summary["latency_ms_all_attempts"]["p95"] > 500

    two = [call(0, "standard_first", 0, "standard", latency_ms=100)]
    two += [call(0, "standard_first", 1, "standard", latency_ms=200)]
    latency = script.summarize([{"meta": META}, *two])["latency_ms_answered"]
    assert latency["p50"] <= latency["p95"]


def test_sample_takes_a_fixed_number_per_intent() -> None:
    examples = EXAMPLES + [Example("x", "age_limit", "dev", 9)]
    chosen = script.sample(examples, 2)
    assert sorted(e.intent for e in chosen) == [
        "age_limit",
        "card_arrival",
        "card_arrival",
    ]
    assert script.sample(examples, 2) == chosen


def test_the_headline_does_not_depend_on_which_order_lost_a_call() -> None:
    # Review on #90: with P(complex) 0.4 in one order and 0.1 in the other
    # (true excess 0.09), losing calls in one order changed the headline's sign.
    n = 800

    def headline(lost: str | None) -> float:
        rng = random.Random(3)
        calls = [{"meta": {**META, "messages": n}}]
        for row in range(n):
            for order, p_complex in (("standard_first", 0.4), ("complex_first", 0.1)):
                for k in range(3):
                    if order == lost and k == 2 and row % 2:
                        continue
                    route = "complex" if rng.random() < p_complex else "standard"
                    calls.append(call(row, order, k, route))
        return script.summarize(calls)["flip_minus_same_order"]["mean"]

    for lost in (None, *script.ORDERS):
        assert headline(lost) > 0.04, lost


def test_same_order_disagreement_weights_the_orders_equally() -> None:
    orders = {
        "standard_first": [
            call(0, "standard_first", k, route)
            for k, route in enumerate(["standard", "complex", "standard"])
        ],
        "complex_first": [call(0, "complex_first", k, "standard") for k in range(2)],
    }
    # standard_first: 2 of 3 pairs differ; complex_first: 0 of 1. Mean 1/3,
    # not the pooled 2/4.
    assert script.same_order(orders) == pytest.approx(1 / 3)


def test_the_verdict_gates_on_the_headlines_own_n() -> None:
    # 150 messages answered in both orders, but only 20 keep two calls in each.
    n = 150
    meta = {**META, "messages": n}
    calls: list[dict[str, Any]] = [{"meta": meta}]
    for row in range(n):
        for order in script.ORDERS:
            for k in range(2 if row < 20 else 1):
                calls.append(call(row, order, k, first_listed(order)))
    summary = script.summarize(calls)

    assert summary["messages_answered_in_both_orders"] == n
    assert summary["flip_minus_same_order"]["n"] == 20
    assert summary["verdict"].startswith("inconclusive: too few")


def test_the_verdict_gates_on_the_headline_not_on_one_call_per_order() -> None:
    # Review on #90: 93% of messages had one answered call in each order, but
    # the headline used only those with two. A model that ignores order lost
    # half its "complex" calls in standard_first and looked biased.
    calls = []
    for row in range(1000):
        if row < 500:  # all four calls answered, and the first option wins
            for order in script.ORDERS:
                for repeat in range(2):
                    calls.append(call(row, order, repeat, first_listed(order)))
        else:  # one call per order only, and they disagree: a flip
            calls.append(call(row, "standard_first", 0, "standard"))
            calls.append(call(row, "complex_first", 0, "complex"))
    meta = {**META, "messages": 1000}
    summary = script.summarize([{"meta": meta}, *calls])

    assert summary["paired_coverage"] == 1.0
    assert summary["headline_coverage"] == 0.5
    assert summary["verdict"].startswith("inconclusive")


def test_the_budget_message_says_to_rerun_with_the_same_file(tmp_path: Path) -> None:
    out = tmp_path / "calls.jsonl"
    with pytest.raises(script.Stopped, match="same --out"):
        run(out, lambda order: order[0], meta={**META, "max_calls": 3})
