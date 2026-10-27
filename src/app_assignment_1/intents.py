"""Keyword matching over the configured intents, shared by the mocks."""

import re
from pathlib import Path

from pydantic import TypeAdapter

from app_assignment_1.schemas import IntentConfig


def words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def load_intents(path: Path) -> list[IntentConfig]:
    intents = TypeAdapter(list[IntentConfig]).validate_json(path.read_text())
    names = [item.intent for item in intents]
    if not names or len(names) != len(set(names)) or "unknown" in names:
        raise ValueError("Intent names must be nonempty, unique and not 'unknown'")
    if any(not words(k) for item in intents for k in item.keywords):
        raise ValueError("Keywords must contain letters or numbers")
    return intents


def match_intent(
    intents: list[IntentConfig], message: str
) -> tuple[int, IntentConfig | None]:
    """Return the best keyword score and intent, or (0, None) with no match.

    Long matching phrases win. Config order resolves ties deterministically.
    """
    text = f" {words(message)} "
    score, match = max(
        (
            (
                sum(
                    len(words(k).split())
                    for k in item.keywords
                    if f" {words(k)} " in text
                ),
                item,
            )
            for item in intents
        ),
        key=lambda pair: pair[0],
    )
    return (score, match) if score else (0, None)
