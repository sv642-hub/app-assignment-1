"""The Jev route-order experiment: does swapping the two route options change
Jev's route? Run by hand with a real TypeSafe key.

    uv run python scripts/evaluate_jev_order.py run --out results/jev_order.jsonl \\
        --max-calls 1200 [--per-intent 4] [--repeats 2] [--concurrency 4] \\
        [--timeout 15] [--max-errors-in-a-row 10]
    uv run python scripts/evaluate_jev_order.py summarize results/jev_order.jsonl

`run` sends the gateway's own Jev request (`jev.py`: model, both questions, all
78 labels) for messages from the dev split (data/banking77_split.csv), once
with `standard` listed first and once with `complex` first, `--repeats` times
each. Only the order of the two route options differs. Calls run in a seeded
random order, never one derived from message text. Each call attempt is one
JSON line keyed by the message's `source_row`, without its text.

The first line records the settings, including the timeout, the call budget
and a hash of each order's questions. A rerun with the same settings resumes;
other settings are refused. Only answered calls count as done, so a rerun
retries calls that failed (timeouts, 429, 5xx, bad answers); there are no
retries within a run. Every attempt, failed or not, counts toward
`--max-calls`, across reruns. The run stops on a 401, or after
`--max-errors-in-a-row` failures in a row. A line cut off by a kill is dropped
and its call redone.

`summarize` prints the measures from the position-bias proposal (#86), each
as {mean, low, high, n} with a 95% bootstrap interval that resamples messages,
keeping each message's calls together. Every measure uses only messages with
answered calls in both orders and gives both orders equal weight, so calls
lost in one order cannot fake a bias. The result is the flip rate minus the
same-order disagreement: a flip only points to order bias if it happens more
often than two calls in the same order disagree. The verdict is
"inconclusive" when that interval covers 0, fewer than MIN_MESSAGES messages
have both orders, or under MIN_PAIRED_COVERAGE of the sampled messages do.
TypeSafe does not document that criteria order reaches the model, so a
difference near 0 also fits order being ignored.

The key comes from APP_JEV_API_KEY (environment or env/.env) and is never
printed or saved.
"""

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from itertools import combinations, product
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from pydantic import ValidationError

from app_assignment_1.banking77 import SPLIT_PATH, Example, load_split
from app_assignment_1.config import Settings
from app_assignment_1.jev import (
    ROUTE_CRITERIA,
    JevCallError,
    TypeSafeJevRouter,
    build_questions,
    create_jev_http_client,
)
from app_assignment_1.labels import LabelSet, load_labels

ROOT = Path(__file__).resolve().parent.parent
ORDERS = {
    "standard_first": ("standard", "complex"),
    "complex_first": ("complex", "standard"),
}
SEED = 6700
RESAMPLES = 2000
MAX_CONCURRENCY = 16
# Below these the verdict is "inconclusive", whatever the interval says. With
# fewer messages the bootstrap interval is too narrow (a simulated order-blind
# model fell outside it in 17 of 100 runs at 30 messages, 9 at 100).
MIN_MESSAGES = 100
MIN_PAIRED_COVERAGE = 0.9

Record = dict[str, Any]
Orders = dict[str, list[Record]]  # a message's answered calls, by order


class Stopped(Exception):
    """The run stopped early: a 401, too many errors in a row, or the budget."""


def questions_for(labels: LabelSet, order: str) -> dict[str, Any]:
    questions = build_questions(labels)
    questions["route"]["criteria"] = {
        name: ROUTE_CRITERIA[name] for name in ORDERS[order]
    }
    return questions


def questions_sha256(labels: LabelSet) -> dict[str, str]:
    """Pins the wording and the option order each order sends."""
    return {
        order: hashlib.sha256(
            json.dumps(questions_for(labels, order)).encode()
        ).hexdigest()
        for order in ORDERS
    }


def sample(examples: Sequence[Example], per_intent: int | None) -> list[Example]:
    if per_intent is None:
        return list(examples)
    by_intent: dict[str, list[Example]] = defaultdict(list)
    for example in examples:
        by_intent[example.intent].append(example)
    rng = random.Random(SEED)
    chosen = []
    for intent in sorted(by_intent):
        rows = by_intent[intent]
        rng.shuffle(rows)
        chosen += rows[:per_intent]
    return chosen


def read_records(path: Path, repair: bool = False) -> list[Record]:
    """The file's lines; a cut-off last line is skipped (and removed if
    `repair`). A broken line anywhere else is an error."""
    lines = path.read_text().splitlines()
    records = []
    for number, line in enumerate(lines, 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if number != len(lines):
                raise ValueError(f"{path} line {number} is not JSON") from None
            if repair:
                path.write_text("".join(f"{line}\n" for line in lines[:-1]))
    return records


async def call_once(
    router: TypeSafeJevRouter, example: Example, order: str, repeat: int
) -> Record:
    record: Record = {
        "row": example.source_row,
        "intent": example.intent,
        "order": order,
        "repeat": repeat,
    }
    start = perf_counter()
    try:
        result = await router.route(example.text)
    except JevCallError as error:
        latency = round((perf_counter() - start) * 1000)
        return record | {"error": error.error_type, "latency_ms": latency}
    return record | {
        "error": result.route.error_type,
        "route": result.route.choice,
        "p_standard": (result.route.probabilities or {}).get("standard"),
        "intent_choice": result.intent.choice,
        "route_order": result.route_order,
        "jev_model": result.jev_model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": round((perf_counter() - start) * 1000),
    }


async def run_experiment(
    out: Path,
    examples: Sequence[Example],
    meta: Record,
    settings: Settings,
    labels: LabelSet,
    concurrency: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Make the calls not yet answered in `out`; returns how many were made.

    `meta` must hold `repeats`, `seed`, `max_calls` and `max_errors_in_a_row`.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1 to {MAX_CONCURRENCY}")
    saved: list[Record] = []
    if out.exists() and out.stat().st_size:
        first, *saved = read_records(out, repair=True) or [{}]
        if first.get("meta") != meta:
            raise ValueError(f"{out} was made with other settings; use a new --out")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"meta": meta}) + "\n")
    done = {(r["row"], r["order"], r["repeat"]) for r in saved if r["error"] is None}
    budget = meta["max_calls"] - len(saved)

    calls = [
        (example, order, repeat)
        for example in examples
        for order in ORDERS
        for repeat in range(meta["repeats"])
        if (example.source_row, order, repeat) not in done
    ]
    random.Random(meta["seed"]).shuffle(calls)
    semaphore = asyncio.Semaphore(concurrency)
    made = 0
    errors_in_a_row = 0
    stop_reason: str | None = None

    async with create_jev_http_client(settings, transport) as client:
        routers = {}
        for order in ORDERS:
            routers[order] = TypeSafeJevRouter(client, settings.jev_model, labels)
            routers[order].questions = questions_for(labels, order)

        with out.open("a") as handle:

            async def one(example: Example, order: str, repeat: int) -> None:
                nonlocal made, errors_in_a_row, stop_reason
                async with semaphore:
                    if stop_reason:
                        return
                    if made >= budget:
                        stop_reason = (
                            f"the call budget ({meta['max_calls']}) is spent; "
                            "raise --max-calls with a new --out to go on"
                        )
                        return
                    made += 1
                    record = await call_once(routers[order], example, order, repeat)
                    if record["error"] == "http_401":
                        stop_reason = "Jev answered 401: check APP_JEV_API_KEY"
                        return
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    if record["error"] is None:
                        errors_in_a_row = 0
                        return
                    errors_in_a_row += 1
                    if errors_in_a_row >= meta["max_errors_in_a_row"] and (
                        not stop_reason
                    ):
                        stop_reason = (
                            f"{errors_in_a_row} failed calls in a row (last: "
                            f"{record['error']}); rerun later to retry them"
                        )

            await asyncio.gather(*(one(*call) for call in calls))
    if stop_reason:
        raise Stopped(f"Stopped after {made} calls: {stop_reason}")
    return made


def interval(values: Sequence[float]) -> dict[str, float | int] | None:
    """Mean with a 95% percentile bootstrap interval over messages."""
    if not values:
        return None
    rng = random.Random(SEED)
    means = statistics.quantiles(
        (
            statistics.fmean(rng.choices(values, k=len(values)))
            for _ in range(RESAMPLES)
        ),
        n=40,
        method="inclusive",
    )
    return {
        "mean": statistics.fmean(values),
        "low": means[0],
        "high": means[-1],
        "n": len(values),
    }


def percentiles(values: Sequence[float]) -> dict[str, float] | None:
    """p50 and p95 by one rule (`statistics.quantiles`, inclusive)."""
    if not values:
        return None
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0]}
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return {"p50": cuts[49], "p95": cuts[94]}


def flip(orders: Orders) -> float:
    """Share of cross-order call pairs with different routes. Every pair
    counts, so a message keeps its pairs whichever repeats were answered."""
    pairs = product(orders["standard_first"], orders["complex_first"])
    return statistics.fmean(a["route"] != b["route"] for a, b in pairs)


def same_order(orders: Orders) -> float | None:
    """Share of same-order call pairs with different routes."""
    pairs = [
        a["route"] != b["route"]
        for calls in orders.values()
        for a, b in combinations(calls, 2)
    ]
    return statistics.fmean(pairs) if pairs else None


def excess_flip(orders: Orders) -> float | None:
    noise = same_order(orders)
    return None if noise is None else flip(orders) - noise


def order_mean(orders: Orders, field: Callable[[Record], float]) -> list[float]:
    return [statistics.fmean(map(field, orders[order])) for order in ORDERS]


def first_position(orders: Orders) -> float:
    """Each order's first-listed rate, then their mean, so an order-blind
    model gives 0.5 however many calls each order lost."""
    return statistics.fmean(
        order_mean(orders, lambda r: r["route"] == ORDERS[r["order"]][0])
    )


def standard_gap(orders: Orders) -> float:
    first, second = order_mean(orders, lambda r: r["route"] == "standard")
    return first - second


def p_standard_gap(orders: Orders) -> float | None:
    if any(r["p_standard"] is None for calls in orders.values() for r in calls):
        return None
    first, second = order_mean(orders, lambda r: r["p_standard"])
    return first - second


def verdict(result: dict[str, Any] | None, paired: int, coverage: float) -> str:
    if paired < MIN_MESSAGES or coverage < MIN_PAIRED_COVERAGE or result is None:
        return "inconclusive: too few messages answered in both orders"
    if result["low"] <= 0 <= result["high"]:
        return "inconclusive: the interval covers 0"
    if result["low"] > 0:
        return "flips exceed same-order disagreement"
    return "flips fall below same-order disagreement"


def summarize(records: Sequence[Record]) -> dict[str, Any]:
    meta = records[0].get("meta", {}) if records else {}
    attempts = [r for r in records if "meta" not in r]
    answered = [r for r in attempts if r["error"] is None]
    planned = meta.get("messages", 0) * meta.get("repeats", 0)

    by_row: dict[int, Orders] = defaultdict(lambda: {order: [] for order in ORDERS})
    for r in answered:
        by_row[r["row"]][r["order"]].append(r)
    paired = {row: o for row, o in by_row.items() if all(o.values())}
    one_order = len(by_row) - len(paired)
    coverage = len(paired) / meta["messages"] if meta.get("messages") else 0.0

    def per_message(stat: Callable[[Orders], float | None]) -> Any:
        return interval([v for v in map(stat, paired.values()) if v is not None])

    by_order = {
        order: {
            "planned": planned,
            "answered": sum(r["order"] == order for r in answered),
            "failed_attempts": dict(
                Counter(
                    r["error"]
                    for r in attempts
                    if r["order"] == order and r["error"] is not None
                )
            ),
        }
        for order in ORDERS
    }
    result = per_message(excess_flip)
    return {
        "attempts": len(attempts),
        "answered_calls": len(answered),
        "calls_by_order": by_order,
        "messages_sampled": meta.get("messages"),
        "messages_answered_in_both_orders": len(paired),
        "messages_answered_in_one_order_only": one_order,
        "paired_coverage": coverage,
        "flip_minus_same_order": result,
        "verdict": verdict(result, len(paired), coverage),
        "flip_rate": per_message(flip),
        "same_order_disagreement": per_message(same_order),
        "first_position_rate": per_message(first_position),
        "standard_rate_gap": per_message(standard_gap),
        "p_standard_gap": per_message(p_standard_gap),
        "latency_ms_all_attempts": percentiles([r["latency_ms"] for r in attempts]),
        "latency_ms_answered": percentiles([r["latency_ms"] for r in answered]),
        "tokens_per_answered_call": (
            statistics.fmean(r["input_tokens"] + r["output_tokens"] for r in answered)
            if answered
            else None
        ),
    }


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low} to {high}")
        return value

    return parse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument(
        "--max-calls",
        type=bounded_int(1, 100_000),
        required=True,
        help="most call attempts this --out may ever make, reruns included",
    )
    run.add_argument(
        "--per-intent", type=bounded_int(1, 10_000), help="all messages if unset"
    )
    run.add_argument("--repeats", type=bounded_int(1, 10), default=2)
    run.add_argument("--concurrency", type=bounded_int(1, MAX_CONCURRENCY), default=4)
    run.add_argument("--timeout", type=float, default=15, help="seconds, at most 30")
    run.add_argument("--max-errors-in-a-row", type=bounded_int(1, 1000), default=10)
    show = sub.add_parser("summarize")
    show.add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "summarize":
        print(json.dumps(summarize(read_records(args.path)), indent=2))
        return

    try:
        # Built, not copied, so the timeout gets Settings' own checks.
        settings = Settings(jev_timeout_seconds=args.timeout)
    except ValidationError as error:
        sys.exit(f"Bad settings: {error.errors()[0]['msg']}")
    labels = load_labels(ROOT / "config" / "labels.json")
    examples = sample(load_split("dev"), args.per_intent)
    meta = {
        "jev_model": settings.jev_model,
        "option_set_version": labels.option_set_version,
        "questions_sha256": questions_sha256(labels),
        "split_sha256": hashlib.sha256(SPLIT_PATH.read_bytes()).hexdigest(),
        "per_intent": args.per_intent,
        "messages": len(examples),
        "repeats": args.repeats,
        "seed": SEED,
        "timeout_seconds": settings.jev_timeout_seconds,
        "max_calls": args.max_calls,
        "max_errors_in_a_row": args.max_errors_in_a_row,
    }
    try:
        made = asyncio.run(
            run_experiment(args.out, examples, meta, settings, labels, args.concurrency)
        )
    except (ValueError, Stopped) as error:
        sys.exit(str(error))
    print(f"Made {made} calls; saved to {args.out}")


if __name__ == "__main__":
    main()
