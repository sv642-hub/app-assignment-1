"""Small router interface with a deterministic, configuration-driven mock."""

import re
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from app_assignment_1.schemas import IntentConfig, RoutingDecision


def words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


class Router(Protocol):
    async def route(self, message: str) -> RoutingDecision: ...


class MockJevRouter:
    """Keyword scores are demo heuristics, not calibrated model probabilities.

    Long matching phrases win. Config order resolves ties deterministically.
    """

    def __init__(self, path: Path):
        self.intents = TypeAdapter(list[IntentConfig]).validate_json(path.read_text())
        names = [item.intent for item in self.intents]
        if not names or len(names) != len(set(names)) or "unknown" in names:
            raise ValueError("Intent names must be nonempty, unique and not 'unknown'")
        if any(not words(k) for item in self.intents for k in item.keywords):
            raise ValueError("Keywords must contain letters or numbers")

    async def route(self, message: str) -> RoutingDecision:
        text = f" {words(message)} "
        scored = [
            (
                sum(
                    len(words(k).split())
                    for k in item.keywords
                    if f" {words(k)} " in text
                ),
                item,
            )
            for item in self.intents
        ]
        score, match = max(scored, key=lambda pair: pair[0])
        if score == 0:
            return RoutingDecision(route="standard", intent="unknown", confidence=0)
        return RoutingDecision(
            route=match.complexity,
            intent=match.intent,
            confidence=round(min(0.95, 0.55 + score * 0.08), 2),
        )
