"""The shared intent labels that Jev and the Qwen backends classify into.

The 77 Banking77 intents plus `unknown`. Only intents with a policy card get a
policy reply; the rest are recognized but answered with a fixed reply.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from app_assignment_1.schemas import LabelConfig

UNKNOWN = "unknown"


@dataclass(frozen=True)
class LabelSet:
    items: tuple[LabelConfig, ...]
    names: frozenset[str]
    # Answers are only comparable within one option set. Derived from the
    # labels, their descriptions and their order, which Jev sees.
    option_set_version: str


def load_labels(path: Path) -> LabelSet:
    items = TypeAdapter(list[LabelConfig]).validate_json(path.read_text())
    names = [item.label for item in items]
    if len(names) != len(set(names)):
        raise ValueError("Labels must be unique")
    if names.count(UNKNOWN) != 1:
        raise ValueError("Labels must include 'unknown' exactly once")
    for item in items:
        if (item.banking77_name is None) != (item.label == UNKNOWN):
            raise ValueError("Every label except 'unknown' needs its Banking77 name")
    canonical = json.dumps(
        [item.model_dump() for item in items], sort_keys=True, ensure_ascii=False
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return LabelSet(tuple(items), frozenset(names), f"labels-{digest}")
