"""Intent routing: the interface the gateway calls, and a mock of Jev.

The real Jev client will be a second class with the same `route` method, so the
gateway does not change when it replaces MockJevRouter.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from smart_gateway.catalog import IntentSpec, Route

UNKNOWN_INTENT = "unknown"


class RouteDecision(BaseModel):
    """What a router returns for one message."""

    route: Route
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class IntentRouter(Protocol):
    """Anything that can label a message with an intent and a route."""

    name: str

    async def route(self, message: str) -> RouteDecision: ...


def _normalize(text: str) -> str:
    return text.lower().replace("’", "'")


class MockJevRouter:
    """Keyword stand-in for Jev, driven by config/intents.json.

    Each matched keyword scores its word count, so longer phrases count for
    more. The best-scoring intent wins (ties go to the one listed first), its
    complexity is the route, and its share of the total score is the
    confidence. No match returns the unknown intent with confidence 0.
    """

    name = "mock-jev"

    def __init__(self, intents: list[IntentSpec]) -> None:
        self._patterns = [
            (
                intent,
                [
                    (re.compile(rf"\b{re.escape(_normalize(k))}\b"), len(k.split()))
                    for k in intent.keywords
                ],
            )
            for intent in intents
        ]

    async def route(self, message: str) -> RouteDecision:
        text = _normalize(message)
        scores = [
            (
                intent,
                sum(weight for pattern, weight in patterns if pattern.search(text)),
            )
            for intent, patterns in self._patterns
        ]
        total = sum(score for _, score in scores)
        if total == 0:
            return RouteDecision(route="standard", intent=UNKNOWN_INTENT, confidence=0)

        best, best_score = max(scores, key=lambda pair: pair[1])
        return RouteDecision(
            route=best.complexity,
            intent=best.name,
            confidence=round(best_score / total, 3),
        )
