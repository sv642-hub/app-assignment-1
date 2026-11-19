"""The Banking77 development split for offline evaluation.

Banking77 ships only train and test, and the official test set stays the primary
benchmark, so the development split is carved from train. Per intent, a fixed
share of messages goes to `dev` (routing threshold, probe gate, Jev route-order
experiment, human grades) and the rest to `train` (probe training). The split is
committed as data/banking77_split.csv; scripts/make_banking77_split.py builds it.
"""

import csv
import random
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

SEED = 6700
DEV_SHARE = 0.2
SPLIT_PATH = Path("data/banking77_split.csv")
SPLITS = ("train", "dev")


class Example(NamedTuple):
    text: str
    intent: str  # our label from config/labels.json, not the Banking77 spelling
    split: str
    source_row: int  # 0-based data row in Banking77's train.csv


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def make_split(
    train: Iterable[tuple[str, str]],
    test_texts: Iterable[str],
    intent_for: dict[str, str],
) -> list[Example]:
    """Split Banking77 train rows (text, category), stratified by intent."""
    held_out = {normalize(text) for text in test_texts}
    seen: set[str] = set()
    by_intent: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row, (text, category) in enumerate(train):
        key = normalize(text)
        # Repeats and messages also in the official test set would leak across
        # splits, so they are dropped.
        if key in held_out or key in seen:
            continue
        seen.add(key)
        by_intent[intent_for[category]].append((row, text))

    rng = random.Random(SEED)
    examples = []
    for intent in sorted(by_intent):
        rows = sorted(by_intent[intent])
        rng.shuffle(rows)
        n_dev = round(DEV_SHARE * len(rows))
        for i, (row, text) in enumerate(rows):
            split = "dev" if i < n_dev else "train"
            examples.append(Example(text, intent, split, row))
    return sorted(examples, key=lambda example: example.source_row)


def load_split(split: str, path: Path = SPLIT_PATH) -> list[Example]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    with path.open(newline="", encoding="utf-8") as file:
        return [
            Example(row["text"], row["intent"], row["split"], int(row["source_row"]))
            for row in csv.DictReader(file)
            if row["split"] == split
        ]
