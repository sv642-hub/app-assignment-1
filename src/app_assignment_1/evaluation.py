"""Offline evaluation: datasets, scoring and the oracle router.

The runner (scripts/run_eval.py) sends each message to one target and writes a
JSONL file of results. This module holds the parts that need no network or
model: loading datasets, scoring a results file and combining the small and
large results into the oracle router.

Results never contain message text. Each item is keyed by the dataset and the
message's 0-based row in it, so the text can be looked up again from the
pinned source.

Scoring follows docs/JEV_QWEN_INTEGRATION_PLAN.md:
- Intent accuracy and macro F1. A failed call counts as wrong. A prediction
  of `unknown` is wrong for a Banking77 message, but right for accuracy on a
  message whose gold label is `unknown` (the fresh set has some).
- Macro F1 averages F1 over the Banking77 intents (every label except
  `unknown`) with at least one gold message in the run. On the full official
  test set that is all 77. On a partial set (`--limit`, the dev split, the
  fresh set), the class list depends only on the gold labels, so two targets
  on the same rows are averaged over the same classes, whatever they predict.
  (Counting predicted-only intents, scikit-learn's default, would score a
  wrong guess outside the gold set lower than answering `unknown`.)
- Per-class recall, which is only worth reading where a class has enough
  messages (40 per intent in the official test set).
"""

import csv
import hashlib
import io
import math
import random
import statistics
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_assignment_1.labels import UNKNOWN, LabelSet
from app_assignment_1.schemas import BackendReply
from app_assignment_1.service import check_reply

# Banking77 by PolyAI, CC BY 4.0, at the same pinned commit as the dev split
# (scripts/make_banking77_split.py on #89).
BANKING77_COMMIT = "57ec275d8078af65b7731c2a98be812d844a6d6b"
BANKING77_URL = (
    "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/"
    f"{BANKING77_COMMIT}/banking_data/{{name}}.csv"
)
BANKING77_SHA256 = {
    "test": "d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d",
}
CACHE_DIR = Path("data/cache")


@dataclass(frozen=True)
class Message:
    row: int  # 0-based data row in the dataset's source file
    text: str
    gold: str  # our label from config/labels.json


@dataclass(frozen=True)
class Dataset:
    name: str
    sha256: str  # of the source file, so a result file names exactly what it used
    messages: tuple[Message, ...]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(
    body: bytes, columns: tuple[str, ...]
) -> Iterable[tuple[int, dict[str, str]]]:
    """CSV rows, checked for the columns. Reads a byte order mark (Excel's
    "CSV UTF-8") as part of the encoding, not the first column's name."""
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"The CSV has no {', '.join(missing)} column")
    for row, record in enumerate(reader):
        if any(record[c] is None for c in columns):
            raise ValueError(f"Row {row} is missing a field")
        yield row, record


def parse_banking77(body: bytes, labels: LabelSet) -> list[Message]:
    """Rows of a Banking77 CSV (`text,category`), mapped to our labels.

    Gold labels go through `banking77_name`, not lowercasing: Banking77 spells
    `Refund_not_showing_up` and `reverted_card_payment?` differently.
    """
    by_name = {item.banking77_name: item.label for item in labels.items}
    messages = []
    for row, record in _rows(body, ("text", "category")):
        category = record["category"]
        if category not in by_name:
            raise ValueError(f"Row {row}: {category!r} is not a Banking77 intent")
        messages.append(Message(row, record["text"], by_name[category]))
    return messages


def parse_labelled_csv(
    body: bytes, labels: LabelSet, split: str | None = None
) -> list[Message]:
    """A CSV with `text` and `intent` columns, such as the fresh set.

    `intent` may be one of our labels or a Banking77 name. `out_of_scope`
    maps to `unknown`, as agreed for this release.

    With `split`, the CSV also needs a `split` column, and only the rows with
    that value are kept, such as `dev` from data/banking77_split.csv (#89),
    which holds train and dev in one file. Rows keep their number in the whole
    file, so a result still points at the right line.
    """
    by_name = {item.banking77_name: item.label for item in labels.items}
    columns = ("text", "intent") + (("split",) if split is not None else ())
    messages = []
    for row, record in _rows(body, columns):
        if split is not None and record["split"].strip() != split:
            continue
        intent = record["intent"].strip()
        if intent == "out_of_scope":
            intent = UNKNOWN
        intent = by_name.get(intent, intent)
        if intent not in labels.names:
            raise ValueError(f"Row {row}: {intent!r} is not a label")
        messages.append(Message(row, record["text"], intent))
    if split is not None and not messages:
        raise ValueError(f"No rows have split {split!r}")
    return messages


def stratified_sample(
    messages: Sequence[Message], size: int, seed: int
) -> list[Message]:
    """`size` messages spread evenly over the gold labels, the same every time
    for the same seed.

    Each label's messages are shuffled with the seed, then labels take turns
    giving one message until `size` are chosen, so every label is covered
    before any gets a second. Labels go in sorted order, so which labels get
    one extra when `size` doesn't divide evenly is also fixed. The result is
    in row order.
    """
    if size < 1:
        raise ValueError("The sample size must be at least 1")
    rng = random.Random(seed)
    by_gold: dict[str, list[Message]] = {}
    for message in messages:
        by_gold.setdefault(message.gold, []).append(message)
    queues = []
    for gold in sorted(by_gold):
        group = by_gold[gold][:]
        rng.shuffle(group)
        queues.append(group)
    chosen: list[Message] = []
    while len(chosen) < size and any(queues):
        for queue in queues:
            if queue and len(chosen) < size:
                chosen.append(queue.pop())
    return sorted(chosen, key=lambda message: message.row)


def reply_format_errors(
    reply: BackendReply, labels: LabelSet, cards: Collection[str]
) -> list[str]:
    """The gateway's format checks on one backend reply (see `Gateway._chat`).

    A model called directly gets the same checks as through the gateway, so a
    failed check counts the same way: the customer would have seen the
    fallback apology, which carries the intent `unknown`.
    tests/test_evaluation.py checks this agrees with the gateway.
    """
    intent, facts = reply.intent, reply.policy_facts
    if intent in labels.names and intent != UNKNOWN and intent not in cards:
        # Uncarded: the gateway discards the text and inserts a fixed reply.
        return ["uncarded_policy_facts"] if facts else []
    errors = check_reply(reply.reply)
    if intent not in labels.names:
        errors.append("unknown_label")
    elif intent == UNKNOWN and facts:
        errors.append("unknown_policy_facts")
    return errors


def gold_classes(labels: LabelSet) -> list[str]:
    """The 77 Banking77 intents: every label except `unknown`."""
    return [item.label for item in labels.items if item.label != UNKNOWN]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a share k/n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def score(items: Sequence[dict[str, Any]], labels: LabelSet) -> dict[str, Any]:
    """Score result items (`gold`, `predicted`, `error`, `latency_ms`, ...).

    `predicted` is None for a failed call; it counts as wrong.

    Macro F1 averages over the intents with at least one gold message, so on a
    partial set every target is averaged over the same classes. A wrong guess
    still costs: it lowers the gold intent's recall and, if the guess is a
    gold intent too, that intent's precision. On the full test set all 77
    intents have gold messages.
    """
    present = {item["gold"] for item in items}
    classes = [label for label in gold_classes(labels) if label in present]
    n = len(items)
    correct = sum(item["predicted"] == item["gold"] for item in items)
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    support: Counter[str] = Counter()
    for item in items:
        gold, predicted = item["gold"], item["predicted"]
        support[gold] += 1
        if predicted == gold:
            tp[gold] += 1
        else:
            fn[gold] += 1
            if predicted is not None:
                fp[predicted] += 1
    f1 = {}
    recall = {}
    for label in classes:
        precision_d = tp[label] + fp[label]
        recall_d = tp[label] + fn[label]
        p = tp[label] / precision_d if precision_d else 0.0
        r = tp[label] / recall_d if recall_d else 0.0
        f1[label] = 2 * p * r / (p + r) if p + r else 0.0
        if support[label]:
            recall[label] = r
    latencies = [item["latency_ms"] for item in items if item.get("error") is None]
    low, high = wilson(correct, n)
    return {
        "messages": n,
        "correct": correct,
        "accuracy": correct / n if n else None,
        "accuracy_95ci": [low, high],
        "macro_f1": statistics.fmean(f1.values()) if classes else None,
        "macro_f1_classes": len(classes),
        "errors": dict(Counter(item["error"] for item in items if item.get("error"))),
        "format_failures": sum(item.get("format_ok") is False for item in items),
        "predicted_unknown": sum(item["predicted"] == UNKNOWN for item in items),
        "models": dict(Counter(item["model"] for item in items if item.get("model"))),
        "latency_ms_p50": percentile(latencies, 0.5),
        "latency_ms_p95": percentile(latencies, 0.95),
        "per_class_recall": recall,
        "per_class_support": {label: support[label] for label in recall},
    }


def oracle(
    small: Iterable[dict[str, Any]], large: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The oracle router: the large model only where it is right and the small
    model is wrong; the small model everywhere else, including where both are
    wrong (the cheaper wrong answer). Its accuracy is an upper bound for any
    router over these two models, and its large share is the least use of the
    large model that reaches it. It makes no calls of its own. Both inputs must
    cover the same rows."""
    by_row = {item["row"]: item for item in large}
    combined = []
    for item in small:
        other = by_row.pop(item["row"], None)
        if other is None or other["gold"] != item["gold"]:
            raise ValueError(f"Row {item['row']} differs between the two results")
        helps = item["predicted"] != item["gold"] and other["predicted"] == item["gold"]
        pick = other if helps else item
        combined.append({**pick, "row": item["row"]})
    if by_row:
        raise ValueError(f"{len(by_row)} rows are only in the large results")
    return combined
