"""Router interface: Jev (mocked here) and the rule router choose a route only."""

from pathlib import Path
from typing import Protocol

from app_assignment_1.intents import load_intents, match_intent, words
from app_assignment_1.schemas import RoutingDecision


class Router(Protocol):
    async def route(self, message: str) -> RoutingDecision: ...


class MockJevRouter:
    """Stands in for Jev's difficulty call.

    Uses the mock complexity of the best keyword match; unmatched messages go to
    the standard route. Scores are demo heuristics, not calibrated probabilities.
    """

    def __init__(self, path: Path):
        self.intents = load_intents(path)

    async def route(self, message: str) -> RoutingDecision:
        score, match = match_intent(self.intents, message)
        if match is None:
            return RoutingDecision(route="standard", confidence=0.5)
        return RoutingDecision(
            route=match.complexity,
            confidence=round(min(0.95, 0.55 + score * 0.08), 2),
        )


class RuleRouter:
    """Fallback when Jev fails or times out: long messages go to the large model."""

    def __init__(self, complex_min_words: int):
        self.complex_min_words = complex_min_words

    async def route(self, message: str) -> RoutingDecision:
        long = len(words(message).split()) >= self.complex_min_words
        return RoutingDecision(route="complex" if long else "standard")
