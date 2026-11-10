"""Router interface: one Jev call answers the route and intent questions.

The rule router is the fallback and chooses a route only.
"""

from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from app_assignment_1.intents import load_intents, match_intent, words
from app_assignment_1.labels import UNKNOWN
from app_assignment_1.records import ROUTE_OPTIONS, check_probabilities
from app_assignment_1.schemas import RoutingDecision, StrictModel


class JevAnswer(StrictModel):
    """One question's answer, validated on its own.

    A valid answer has a choice, confidence and probabilities. An invalid one
    has only `error_type`, a short code that never holds provider text.
    """

    choice: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] | None = None
    error_type: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def check_answer(self) -> "JevAnswer":
        answer = (self.choice, self.confidence, self.probabilities)
        if self.error_type is not None:
            if any(value is not None for value in answer):
                raise ValueError("An invalid answer has no choice")
            return self
        if self.choice is None or self.confidence is None or self.probabilities is None:
            raise ValueError(
                "A valid answer needs a choice, confidence and probabilities"
            )
        check_probabilities(self.choice, self.probabilities)
        return self


class JevResult(StrictModel):
    """Both answers from one combined Jev call, and the call's details.

    Internal to the gateway: it picks the route from `route` and, from step B3,
    writes a `JevAssessment` for each answer. The intent options are checked
    against the label file when the records are written.
    """

    route: JevAnswer
    intent: JevAnswer
    mocked: bool
    jev_model: str = Field(min_length=1)  # e.g. jev-1.14.0
    provider_request_id: str | None = None  # x-typesafe-request-id, if present
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def check_route_options(self) -> "JevResult":
        probabilities = self.route.probabilities
        if probabilities is not None and set(probabilities) != ROUTE_OPTIONS:
            raise ValueError("The route answer chooses standard or complex")
        return self


class Router(Protocol):
    """Jev's interface. A whole-call failure raises; the gateway then uses the
    rule router. An invalid answer to one question comes back as a `JevAnswer`
    with `error_type` set."""

    async def route(self, message: str) -> JevResult: ...


class MockJevRouter:
    """Stands in for the combined Jev call.

    Uses the best keyword match: its configured complexity for the route and its
    intent for the intent answer. Unmatched messages go to the standard route
    with intent `unknown`. Confidence and probabilities are demo heuristics, not
    calibrated, and the mock reports no tokens.
    """

    def __init__(self, path: Path):
        self.intents = load_intents(path)
        self.intent_options = [item.intent for item in self.intents] + [UNKNOWN]

    async def route(self, message: str) -> JevResult:
        score, match = match_intent(self.intents, message)
        if match is None:
            route, intent, confidence = "standard", UNKNOWN, 0.5
        else:
            route, intent = match.complexity, match.intent
            confidence = round(min(0.95, 0.55 + score * 0.08), 2)
        other = "complex" if route == "standard" else "standard"
        return JevResult(
            route=JevAnswer(
                choice=route,
                confidence=confidence,
                probabilities={route: confidence, other: round(1 - confidence, 2)},
            ),
            intent=JevAnswer(
                choice=intent,
                confidence=confidence,
                probabilities=self._spread(intent, confidence),
            ),
            mocked=True,
            jev_model="mock-jev",
            input_tokens=0,
            output_tokens=0,
        )

    def _spread(self, choice: str, confidence: float) -> dict[str, float]:
        """Give the choice `confidence` and share the rest among the others."""
        rest = (1 - confidence) / (len(self.intent_options) - 1)
        return {
            option: confidence if option == choice else rest
            for option in self.intent_options
        }


class RuleRouter:
    """Fallback when Jev fails or times out: long messages go to the large model."""

    def __init__(self, complex_min_words: int):
        self.complex_min_words = complex_min_words

    async def route(self, message: str) -> RoutingDecision:
        long = len(words(message).split()) >= self.complex_min_words
        return RoutingDecision(route="complex" if long else "standard")
