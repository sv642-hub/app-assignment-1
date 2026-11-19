"""The Jev route-order experiment: does swapping the two route options change
Jev's route? Run by hand with a real TypeSafe key.

    uv run python scripts/evaluate_jev_order.py run --out results/jev_order.jsonl \\
        [--per-intent 4] [--repeats 2] [--concurrency 4] [--timeout 15]
    uv run python scripts/evaluate_jev_order.py summarize results/jev_order.jsonl

`run` sends the gateway's own Jev request (`jev.py`: model, both questions, all
78 labels) for messages from the dev split (data/banking77_split.csv), once
with `standard` listed first and once with `complex` first, `--repeats` times
each. Only the order of the two route options differs. Calls run in a seeded
random order, never one derived from message text. Each call is one JSON line
keyed by the message's `source_row`, without its text. The first line records
the settings; rerunning with the same settings skips saved calls, so an
interrupted run resumes. The run stops on a 401.

`summarize` prints the measures from the position-bias proposal (#86) with 95%
bootstrap intervals that resample messages, keeping each message's calls
together. TypeSafe does not document that criteria order reaches the model: a
flip rate no higher than the same-order disagreement fits order being ignored.

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
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

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

Record = dict[str, Any]


class Unauthorized(Exception):
    pass


def questions_for(labels: LabelSet, order: str) -> dict[str, Any]:
    questions = build_questions(labels)
    questions["route"]["criteria"] = {
        name: ROUTE_CRITERIA[name] for name in ORDERS[order]
    }
    return questions


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
    """Make the calls not yet saved in `out`; returns how many were made."""
    done = set()
    if out.exists():
        first, *saved = (json.loads(line) for line in out.read_text().splitlines())
        if first.get("meta") != meta:
            raise ValueError(f"{out} was made with other settings; use a new --out")
        done = {(r["row"], r["order"], r["repeat"]) for r in saved}
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"meta": meta}) + "\n")

    calls = [
        (example, order, repeat)
        for example in examples
        for order in ORDERS
        for repeat in range(meta["repeats"])
        if (example.source_row, order, repeat) not in done
    ]
    random.Random(meta["seed"]).shuffle(calls)
    semaphore = asyncio.Semaphore(concurrency)
    stopped = False

    async with create_jev_http_client(settings, transport) as client:
        routers = {}
        for order in ORDERS:
            routers[order] = TypeSafeJevRouter(client, settings.jev_model, labels)
            routers[order].questions = questions_for(labels, order)

        with out.open("a") as handle:

            async def one(example: Example, order: str, repeat: int) -> None:
                nonlocal stopped
                async with semaphore:
                    if stopped:
                        return
                    record = await call_once(routers[order], example, order, repeat)
                    if record["error"] == "http_401":
                        stopped = True
                        return
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()

            await asyncio.gather(*(one(*call) for call in calls))
    if stopped:
        raise Unauthorized("Jev answered 401: check APP_JEV_API_KEY")
    return len(calls)


def interval(values: Sequence[float]) -> tuple[float, float, float, int] | None:
    """Mean with a 95% percentile bootstrap interval over messages."""
    if not values:
        return None
    rng = random.Random(SEED)
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values))) for _ in range(RESAMPLES)
    )
    low, high = means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]
    return statistics.fmean(values), low, high, len(values)


def summarize(records: Sequence[Record]) -> dict[str, Any]:
    calls = [r for r in records if "meta" not in r]
    valid = [r for r in calls if r["error"] is None]
    by_row: dict[int, dict[str, dict[int, Record]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for r in valid:
        by_row[r["row"]][r["order"]][r["repeat"]] = r

    def per_message(
        stat: Callable[[dict[str, dict[int, Record]]], float | None],
    ) -> Any:
        return interval([v for v in map(stat, by_row.values()) if v is not None])

    def flip(orders: dict[str, dict[int, Record]]) -> float | None:
        first, second = orders["standard_first"], orders["complex_first"]
        pairs = [
            first[k]["route"] != second[k]["route"]
            for k in first.keys() & second.keys()
        ]
        return statistics.fmean(pairs) if pairs else None

    def same_order(orders: dict[str, dict[int, Record]]) -> float | None:
        pairs = [
            a["route"] != b["route"]
            for repeats in orders.values()
            for a, b in combinations(repeats.values(), 2)
        ]
        return statistics.fmean(pairs) if pairs else None

    def first_position(orders: dict[str, dict[int, Record]]) -> float:
        picks = [
            r["route"] == ORDERS[r["order"]][0]
            for o in orders.values()
            for r in o.values()
        ]
        return statistics.fmean(picks)

    def standard_gap(field: Callable[[Record], float]) -> Callable[..., float | None]:
        def gap(orders: dict[str, dict[int, Record]]) -> float | None:
            first, second = orders["standard_first"], orders["complex_first"]
            if not first or not second:
                return None
            return statistics.fmean(map(field, first.values())) - statistics.fmean(
                map(field, second.values())
            )

        return gap

    latencies = sorted(r["latency_ms"] for r in valid)
    complete = sum(
        1 for o in by_row.values() if o["standard_first"] and o["complex_first"]
    )
    return {
        "calls": len(calls),
        "valid_calls": len(valid),
        "errors": dict(Counter(r["error"] for r in calls if r["error"] is not None)),
        "messages": len({r["row"] for r in calls}),
        "messages_with_both_orders": complete,
        "flip_rate": per_message(flip),
        "same_order_disagreement": per_message(same_order),
        "first_position_rate": per_message(first_position),
        "standard_rate_gap": per_message(
            standard_gap(lambda r: r["route"] == "standard")
        ),
        "p_standard_gap": per_message(standard_gap(lambda r: r["p_standard"])),
        "latency_ms_p50_p95": (
            [
                latencies[len(latencies) // 2],
                latencies[int(0.95 * (len(latencies) - 1))],
            ]
            if latencies
            else None
        ),
        "tokens_per_call": (
            statistics.fmean(r["input_tokens"] + r["output_tokens"] for r in valid)
            if valid
            else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--per-intent", type=int, help="messages per intent; all if unset")
    run.add_argument("--repeats", type=int, default=2)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout", type=float, default=15)
    show = sub.add_parser("summarize")
    show.add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "summarize":
        records = [json.loads(line) for line in args.path.read_text().splitlines()]
        print(
            json.dumps({"meta": records[0].get("meta"), **summarize(records)}, indent=2)
        )
        return

    labels = load_labels(ROOT / "config" / "labels.json")
    settings = Settings().model_copy(update={"jev_timeout_seconds": args.timeout})
    split = ROOT / SPLIT_PATH
    examples = sample(load_split("dev", split), args.per_intent)
    meta = {
        "jev_model": settings.jev_model,
        "option_set_version": labels.option_set_version,
        "split_sha256": hashlib.sha256(split.read_bytes()).hexdigest(),
        "per_intent": args.per_intent,
        "messages": len(examples),
        "repeats": args.repeats,
        "seed": SEED,
    }
    try:
        made = asyncio.run(
            run_experiment(args.out, examples, meta, settings, labels, args.concurrency)
        )
    except (ValueError, Unauthorized) as error:
        sys.exit(str(error))
    print(f"Made {made} calls; saved to {args.out}")


if __name__ == "__main__":
    main()
