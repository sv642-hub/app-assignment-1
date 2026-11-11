"""B6: the live Jev check, run by hand with a real TypeSafe key.

    uv run python scripts/jev_live_check.py sample \\
        --train live-checks/train.csv --out live-checks/sample.json
    uv run python scripts/jev_live_check.py run --train live-checks/train.csv \\
        --sample live-checks/sample.json --out live-checks/calls.jsonl [--limit 20]
    uv run python scripts/jev_live_check.py summarize live-checks/calls.jsonl \\
        --sample live-checks/sample.json

Keep these files in `live-checks/`, which git ignores.

`sample` draws messages from the Banking77 training split (a CSV with `text`
and `category` columns), spread across all 77 intents and three length bands.
It saves row numbers, intents and word counts, never the text. The test split
is kept for evaluation and the dev split for reply grades, so neither is used.

`run` sends the gateway's own request (`jev.py`: model, both questions, all 78
labels) once per sampled message, one call at a time. The client waits up to
`--timeout` seconds (default 15), not the gateway's 2, so slow calls show their
real latency. Each call is one JSON line without the message text. The run
stops on a 401 and after 5 failed calls in a row.

`summarize` prints the measurements: returned model, request-ID header, latency
against the gateway's timeout, probability rounding, the confidence formula,
the gateway's answer checks and tokens.

The key comes from APP_JEV_API_KEY (environment or env/.env) and is never
printed or saved.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from app_assignment_1.config import Settings
from app_assignment_1.jev import (
    REQUEST_ID_HEADER,
    SYSTEM_ONE_PATH,
    _answer,
    build_questions,
    create_jev_http_client,
)
from app_assignment_1.labels import LabelSet, load_labels
from app_assignment_1.records import ROUTE_OPTIONS, probability_total_tolerance

BANDS = ("<=10", "11-20", ">20")
QUESTIONS = ("route", "intent")
GATEWAY_TIMEOUT_SECONDS = 2.0
# Allows for floating-point error on top of the rounding tolerance.
EPSILON = 1e-9
STOP_AFTER_ERRORS = 5


def word_band(words: int) -> str:
    return BANDS[0] if words <= 10 else BANDS[1] if words <= 20 else BANDS[2]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_train(path: Path) -> list[tuple[str, str]]:
    """(text, category) for each data row, in file order."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"text", "category"} <= set(reader.fieldnames):
            raise ValueError("The training CSV needs 'text' and 'category' columns")
        return [(row["text"], row["category"]) for row in reader]


def banking77_intents(labels: LabelSet) -> list[str]:
    return [item.banking77_name for item in labels.items if item.banking77_name]


def band_shares(rows: Sequence[tuple[str, str]]) -> dict[str, float]:
    """Each length band's share of the whole training split."""
    counts = Counter(word_band(len(text.split())) for text, _ in rows)
    return {band: counts[band] / len(rows) for band in BANDS}


def reweighted_share(
    records: Sequence[dict[str, Any]],
    over: Callable[[dict[str, Any]], bool],
    shares: dict[str, float],
) -> tuple[float, float, float] | None:
    """The share over the timeout if messages came in the training split's
    length mix: each band's rate weighted by its share. The interval weights
    each band's Wilson bounds the same way, so it is wider than exact."""
    estimate = low = high = 0.0
    for band in BANDS:
        in_band = [r for r in records if r["band"] == band]
        if not in_band:
            if shares[band] > 0:
                return None
            continue
        k = sum(1 for r in in_band if over(r))
        band_low, band_high = wilson(k, len(in_band))
        estimate += shares[band] * k / len(in_band)
        low += shares[band] * band_low
        high += shares[band] * band_high
    return estimate, low, high


def draw_sample(
    rows: Sequence[tuple[str, str]], intents: Sequence[str], n: int, seed: int
) -> list[dict[str, Any]]:
    """Up to one more message for some intents than others, and within each
    intent, take long, medium and short messages in turn, so the rare long
    messages are not left out. The order is shuffled, so no intent's calls all
    run at the same time of day."""
    if set(category for _, category in rows) != set(intents):
        raise ValueError("The CSV's categories must be the 77 Banking77 intents")
    rng = random.Random(seed)
    base, extra = divmod(n, len(intents))
    plus_one = set(rng.sample(list(intents), extra))
    by_intent: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, (text, category) in enumerate(rows):
        by_intent[category][word_band(len(text.split()))].append(index)
    chosen: list[dict[str, Any]] = []
    for intent in intents:
        quota = base + (intent in plus_one)
        buckets = [list(by_intent[intent][band]) for band in reversed(BANDS)]
        for bucket in buckets:
            rng.shuffle(bucket)
        picked: list[int] = []
        while len(picked) < quota and any(buckets):
            for bucket in buckets:
                if bucket and len(picked) < quota:
                    picked.append(bucket.pop())
        if len(picked) < quota:
            raise ValueError(f"Not enough messages for {intent}")
        chosen += [
            {"row": i, "intent": intent, "words": len(rows[i][0].split())}
            for i in picked
        ]
    rng.shuffle(chosen)
    return chosen


async def run_calls(
    client: httpx.AsyncClient,
    model: str,
    labels: LabelSet,
    messages: Sequence[tuple[dict[str, Any], str]],
    write: Callable[[dict[str, Any]], None],
    clock: Callable[[], float] = perf_counter,
) -> int:
    """One call per (sample item, text). Returns the number of calls made."""
    questions = build_questions(labels)
    options = {
        "route": ROUTE_OPTIONS,
        "intent": frozenset(item.label for item in labels.items),
    }
    errors_in_a_row = 0
    for number, (item, text) in enumerate(messages):
        record: dict[str, Any] = {
            "call": number,
            "row": item["row"],
            "words": item["words"],
            "band": word_band(item["words"]),
        }
        start = clock()
        try:
            response = await client.post(
                SYSTEM_ONE_PATH,
                json={"model": model, "state": text, "questions": questions},
            )
        except httpx.TimeoutException:
            record |= {"status": "timeout", "latency_ms": (clock() - start) * 1000}
        except httpx.RequestError:
            record |= {
                "status": "request_failed",
                "latency_ms": (clock() - start) * 1000,
            }
        else:
            record |= {
                "status": response.status_code,
                "latency_ms": (clock() - start) * 1000,
                # Names only: header values could carry account details.
                "header_names": sorted(response.headers.keys()),
                "request_id": response.headers.get(REQUEST_ID_HEADER),
            }
            if response.status_code == 200:
                record |= _response_fields(response, options)
        write(record)
        ok = record["status"] == 200 and "bad_json" not in record
        errors_in_a_row = 0 if ok else errors_in_a_row + 1
        if record["status"] == 401:
            print("Stopped: TypeSafe rejected the key (401).", file=sys.stderr)
            return number + 1
        if errors_in_a_row >= STOP_AFTER_ERRORS:
            print(
                f"Stopped after {STOP_AFTER_ERRORS} failed calls in a row.",
                file=sys.stderr,
            )
            return number + 1
    return len(messages)


def _response_fields(
    response: httpx.Response, options: dict[str, frozenset[str]]
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"bad_json": True}
    if not isinstance(payload, dict):
        return {"bad_json": True}
    answers = payload.get("answers")
    answers = answers if isinstance(answers, dict) else {}
    fields: dict[str, Any] = {
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "answers": {},
        # What the gateway's own check makes of each answer.
        "error_types": {},
    }
    for question in QUESTIONS:
        raw = answers.get(question)
        fields["answers"][question] = raw
        fields["error_types"][question] = _answer(raw, options[question]).error_type
    return fields


# Summary


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, q from 0 to 1."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for k successes out of n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def decimals(value: float) -> int:
    exponent = Decimal(repr(value)).normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def confidence_tolerances(options: int, step: float) -> tuple[float, float]:
    """How far `confidence` may sit from each prediction when Jev rounds the
    probabilities and the confidence to `step` (0.01 for two decimals).

    The formula's slope in the top probability is n / (n - 1), so rounding p
    moves it by up to that times step/2, and rounding the confidence adds
    step/2: 0.015 for the route question. The top probability itself moves by
    step/2, plus step/2 for the confidence. Returns (formula, top probability).
    """
    half = step / 2
    return options / (options - 1) * half + half + EPSILON, step + EPSILON


def _share(k: int, n: int) -> str:
    if n == 0:
        return "no calls"
    low, high = wilson(k, n)
    return f"{k / n:.1%} ({k} of {n}; 95% CI {low:.1%} to {high:.1%})"


def _probabilities(answer: Any) -> dict[str, float] | None:
    if not isinstance(answer, dict):
        return None
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        return None
    if not all(isinstance(v, int | float) for v in probabilities.values()):
        return None
    return {key: float(value) for key, value in probabilities.items()}


def summarize(
    records: Sequence[dict[str, Any]],
    timeout_seconds: float = GATEWAY_TIMEOUT_SECONDS,
    price_per_million: float | None = None,
    shares: dict[str, float] | None = None,
) -> str:
    """`shares` is each length band's share of the training split, from the
    sample file. The sample takes the bands in turn, so long messages make up
    a larger part of it than of the training split."""
    lines = [f"# Live Jev check: {len(records)} calls", ""]
    statuses = Counter(str(r["status"]) for r in records)
    lines += ["**Status:** " + ", ".join(f"{k}: {v}" for k, v in statuses.items())]
    answered = [r for r in records if r["status"] == 200 and "bad_json" not in r]

    models = Counter(str(r.get("model")) for r in answered)
    lines += [
        "**Model:** "
        + ", ".join(f"`{k}` in {v} of {len(answered)}" for k, v in models.items())
    ]
    with_id = sum(1 for r in records if r.get("request_id"))
    responded = [r for r in records if "header_names" in r]
    names = sorted(
        {
            name
            for r in responded
            for name in r["header_names"]
            if "request" in name or name.endswith("-id")
        }
    )
    lines += [
        f"**`{REQUEST_ID_HEADER}`:** {_share(with_id, len(responded))} of HTTP"
        f" responses. ID-like header names seen: {', '.join(names) or 'none'}."
    ]

    # Failed calls that returned early (429, 5xx, connection errors) are left
    # out of the latency and timeout figures: they say nothing about how long
    # an answer takes, and a burst of them would make the timeout share look
    # lower. The status line above still shows them.
    timed = [r for r in records if r in answered or r["status"] == "timeout"]
    left_out = Counter(
        str(r["status"]) for r in records if "latency_ms" in r and r not in timed
    )
    latencies = [r["latency_ms"] for r in timed]
    if latencies:
        lines += [
            "",
            "## Latency (answered calls and timeouts, ms)",
            "",
            "| p50 | p90 | p95 | p99 | max |",
            "|---|---|---|---|---|",
            "| "
            + " | ".join(
                f"{percentile(latencies, q):.0f}" for q in (0.5, 0.9, 0.95, 0.99)
            )
            + f" | {max(latencies):.0f} |",
            "",
        ]
        if left_out:
            lines += [
                f"Left out: {sum(left_out.values())} failed calls that returned"
                " early (" + ", ".join(f"{k}: {v}" for k, v in left_out.items()) + ").",
                "",
            ]
        limit_ms = timeout_seconds * 1000
        slow = [r for r in timed if r["latency_ms"] > limit_ms]
        lines += [
            f"**Over the gateway's {timeout_seconds:g} s timeout, in this sample:**"
            f" {_share(len(slow), len(timed))}. The plan's trigger is over 5%.",
        ]
        if shares is not None:
            weights = ", ".join(f"{band} {shares[band]:.0%}" for band in BANDS)
            result = reweighted_share(
                timed, lambda r: r["latency_ms"] > limit_ms, shares
            )
            lines += [
                (
                    f"**In the training split's length mix** ({weights}): "
                    + (
                        f"{result[0]:.1%} (95% CI {result[1]:.1%} to"
                        f" {result[2]:.1%}). Compare this one with the trigger."
                        if result
                        else "not available: a band has no calls."
                    )
                )
            ]
        lines += [
            "",
            "| Words | Calls | Over the timeout |",
            "|---|---|---|",
        ]
        for band in BANDS:
            in_band = [r for r in timed if r["band"] == band]
            over = sum(1 for r in in_band if r in slow)
            lines += [f"| {band} | {len(in_band)} | {_share(over, len(in_band))} |"]

    lines += [
        "",
        "## Probabilities",
        "",
        "| Question | Answers | Most decimals | Median off 1 | Max off 1 |"
        " Over 0.05 | Over the tolerance | Confidence = formula |"
        " Confidence = top probability |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tolerance_notes = []
    for question in QUESTIONS:
        rows = []
        for r in answered:
            answer = r["answers"].get(question)
            probabilities = _probabilities(answer)
            if probabilities is not None:
                rows.append((answer, probabilities))
        if not rows:
            lines += [f"| {question} | 0 | | | | | | | |"]
            continue
        offs = [abs(sum(p.values()) - 1) for _, p in rows]
        most = max(decimals(v) for _, p in rows for v in p.values())
        over_fixed = sum(1 for off in offs if off > 0.05)
        over_tol = sum(
            1
            for (_, p), off in zip(rows, offs, strict=True)
            if off > probability_total_tolerance(len(p)) + 1e-9
        )
        # The rounding step comes from the most decimals seen in this
        # question's probabilities and confidences, so full-precision answers
        # still get a tight check.
        confidences = [
            answer["confidence"]
            for answer, _ in rows
            if isinstance(answer.get("confidence"), int | float)
        ]
        step = 10.0 ** -max([most, *(decimals(c) for c in confidences)])
        # The formula and the top probability differ by (1 - top) / (n - 1).
        # Only answers where the two predictions are further apart than both
        # tolerances together can tell them apart: in practice, route.
        formula = top = informative = 0
        for answer, p in rows:
            confidence = answer.get("confidence")
            if not isinstance(confidence, int | float):
                continue
            n = len(p)
            near_formula, near_top = confidence_tolerances(n, step)
            top_p = max(p.values())
            by_formula = (top_p - 1 / n) / (1 - 1 / n)
            if abs(by_formula - top_p) <= near_formula + near_top:
                continue
            informative += 1
            formula += abs(confidence - by_formula) <= near_formula
            top += abs(confidence - top_p) <= near_top
        near_formula, near_top = confidence_tolerances(len(rows[0][1]), step)
        tolerance_notes.append(
            f"{question} ±{near_formula:.3f} for the formula and ±{near_top:.3f}"
            f" for the top probability (step {step:g})"
        )
        lines += [
            f"| {question} | {len(rows)} | {most} | {percentile(offs, 0.5):.3f}"
            f" | {max(offs):.3f} | {over_fixed} | {over_tol}"
            f" | {formula} of {informative} | {top} of {informative} |"
        ]
    if tolerance_notes:
        lines += [
            "",
            "Confidence matches allow for rounding to the most decimals seen: "
            + "; ".join(tolerance_notes)
            + ".",
        ]

    lines += ["", "## The gateway's answer checks", ""]
    for question in QUESTIONS:
        codes = Counter(r["error_types"][question] or "ok" for r in answered)
        lines += [
            f"- **{question}:** " + ", ".join(f"{k}: {v}" for k, v in codes.items())
        ]

    usage = [r["usage"] for r in answered if isinstance(r.get("usage"), dict)]
    inputs = [
        u["input_tokens"] for u in usage if isinstance(u.get("input_tokens"), int)
    ]
    outputs = [
        u["output_tokens"] for u in usage if isinstance(u.get("output_tokens"), int)
    ]
    if inputs and outputs:
        lines += [
            "",
            f"**Tokens per call:** input median {percentile(inputs, 0.5)}, max"
            f" {max(inputs)}; output median {percentile(outputs, 0.5)}, max"
            f" {max(outputs)}.",
        ]
        if price_per_million is not None:
            cost = sum(inputs) * price_per_million / 1_000_000
            lines += [
                f"**Input cost of this run:** ${cost:.4f} at ${price_per_million}"
                " per million input tokens."
            ]
    return "\n".join(lines) + "\n"


# Command line


def _load_sample(path: Path, train: Path) -> list[dict[str, Any]]:
    sample = json.loads(path.read_text())
    if sample["sha256"] != file_digest(train):
        raise ValueError("The training CSV differs from the one the sample used")
    items: list[dict[str, Any]] = sample["items"]
    return items


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("sample")
    s.add_argument("--train", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("-n", type=int, default=300)
    s.add_argument("--seed", type=int, default=6700)
    r = commands.add_parser("run")
    r.add_argument("--train", type=Path, required=True)
    r.add_argument("--sample", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--limit", type=int)
    r.add_argument("--timeout", type=float, default=15)
    m = commands.add_parser("summarize")
    m.add_argument("log", type=Path)
    m.add_argument("--timeout", type=float, default=GATEWAY_TIMEOUT_SECONDS)
    m.add_argument("--sample", type=Path, help="for the length-mix weights")
    args = parser.parse_args(argv)

    settings = Settings()
    labels = load_labels(settings.labels_path)
    if args.command == "sample":
        rows = read_train(args.train)
        items = draw_sample(rows, banking77_intents(labels), args.n, args.seed)
        args.out.write_text(
            json.dumps(
                {
                    "source": args.train.name,
                    "sha256": file_digest(args.train),
                    "seed": args.seed,
                    "band_shares": band_shares(rows),
                    "items": items,
                },
                indent=1,
            )
        )
        bands = Counter(word_band(item["words"]) for item in items)
        print(f"Wrote {len(items)} messages to {args.out}: {dict(bands)}")
        return 0
    if args.command == "run":
        if args.out.exists():
            print(f"{args.out} exists; choose a new file.", file=sys.stderr)
            return 2
        items = _load_sample(args.sample, args.train)[: args.limit]
        rows = read_train(args.train)
        messages = [(item, rows[item["row"]][0]) for item in items]
        try:
            client = create_jev_http_client(
                settings.model_copy(update={"jev_timeout_seconds": args.timeout})
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        with args.out.open("x", encoding="utf-8") as out:

            def write(record: dict[str, Any]) -> None:
                out.write(json.dumps(record) + "\n")
                out.flush()

            async def go() -> int:
                async with client:
                    return await run_calls(
                        client, settings.jev_model, labels, messages, write
                    )

            made = asyncio.run(go())
        print(f"Made {made} calls; log in {args.out}")
        return 0
    records = [json.loads(line) for line in args.log.read_text().splitlines() if line]
    shares = (
        json.loads(args.sample.read_text()).get("band_shares") if args.sample else None
    )
    price = settings.jev_input_price_per_million_usd
    print(summarize(records, args.timeout, price, shares))
    return 0


if __name__ == "__main__":
    sys.exit(main())
