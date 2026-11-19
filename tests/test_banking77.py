"""The Banking77 development split: how it is built and the committed file."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from app_assignment_1.banking77 import SPLIT_PATH, load_split, make_split, normalize
from app_assignment_1.labels import UNKNOWN, load_labels

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "make_banking77_split", ROOT / "scripts" / "make_banking77_split.py"
)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

INTENT_FOR = {"Card_lost": "card_lost", "fee": "fee"}


def sample_train() -> list[tuple[str, str]]:
    rows = [(f"lost card {i}", "Card_lost") for i in range(10)]
    rows += [(f"fee {i}", "fee") for i in range(5)]
    return rows + [("Lost  CARD 3", "Card_lost"), ("in test", "fee")]


def test_split_is_stratified_seeded_and_leak_free() -> None:
    examples = make_split(sample_train(), ["IN TEST"], INTENT_FOR)

    dev = Counter(e.intent for e in examples if e.split == "dev")
    assert dev == {"card_lost": 2, "fee": 1}
    # The repeat of "lost card 3" and the test-set message are dropped.
    assert len(examples) == 15
    assert make_split(sample_train(), ["IN TEST"], INTENT_FOR) == examples


def test_unknown_category_is_an_error() -> None:
    with pytest.raises(KeyError):
        make_split([("hi", "other")], [], INTENT_FOR)


def test_written_split_reads_back(tmp_path: Path) -> None:
    rows = sample_train() + [('a "quoted", multi\nline message', "fee")]
    examples = make_split(rows, [], INTENT_FOR)
    path = tmp_path / "split.csv"
    path.write_text(script.render(examples), encoding="utf-8")

    assert load_split("train", path) + load_split("dev", path) == sorted(
        examples, key=lambda e: e.split, reverse=True
    )
    with pytest.raises(ValueError):
        load_split("test", path)


def test_committed_split_covers_every_intent_without_overlap() -> None:
    train = load_split("train", ROOT / SPLIT_PATH)
    dev = load_split("dev", ROOT / SPLIT_PATH)
    intents = load_labels(ROOT / "config" / "labels.json").names - {UNKNOWN}

    assert {e.intent for e in train} == {e.intent for e in dev} == intents
    assert not {normalize(e.text) for e in train} & {normalize(e.text) for e in dev}
    # Human grades need about 130 replies on the intents with a policy card.
    cards = json.loads((ROOT / "config" / "policy_cards.json").read_text())
    dev_counts = Counter(e.intent for e in dev)
    assert sum(dev_counts[intent] for intent in cards) >= 130
