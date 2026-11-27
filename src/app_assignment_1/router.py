"""Router interface: one Jev call answers the route and intent questions.

The rule router is the fallback and chooses a route only.
"""

import asyncio
import hashlib
import json
import random
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from app_assignment_1.intents import load_intents, match_intent, words
from app_assignment_1.labels import UNKNOWN
from app_assignment_1.records import (
    ROUTE_OPTIONS,
    ROUTE_ORDER,
    check_probabilities,
    check_route_order,
)
from app_assignment_1.schemas import JevFaultConfig, RoutingDecision, StrictModel


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

    Internal to the gateway: it picks the route from `route` and writes a
    `JevAssessment` for each answer. The router checks the intent options: the
    real client against the 78 labels, the mock against its own intents.
    """

    route: JevAnswer
    intent: JevAnswer
    mocked: bool
    jev_model: str = Field(min_length=1)  # e.g. jev-1.14.0
    provider_request_id: str | None = None  # x-typesafe-request-id, if present
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    # The order Jev was shown the route options in, for the position-bias
    # analysis (#86). None when the router does not know it.
    route_order: list[str] | None = None

    @model_validator(mode="after")
    def check_route_options(self) -> "JevResult":
        probabilities = self.route.probabilities
        if probabilities is not None and set(probabilities) != ROUTE_OPTIONS:
            raise ValueError("The route answer chooses standard or complex")
        check_route_order(self.route_order)
        return self


FIT_OPTIONS = frozenset({"fits", "does_not_fit"})


class ReplyCheckResult(StrictModel):
    """The second Jev call: the same route and intent questions, asked about the
    customer's message together with the reply, plus whether the reply fits.

    `fit` chooses between `fits` and `does_not_fit`. Each answer is validated
    on its own, like `JevResult`.
    """

    route: JevAnswer
    intent: JevAnswer
    fit: JevAnswer
    mocked: bool
    jev_model: str = Field(min_length=1)
    provider_request_id: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def check_fit_options(self) -> "ReplyCheckResult":
        probabilities = self.fit.probabilities
        if probabilities is not None and set(probabilities) != FIT_OPTIONS:
            raise ValueError("The fit answer chooses fits or does_not_fit")
        return self


class Router(Protocol):
    """Jev's interface. A whole-call failure raises; the gateway then uses the
    rule router. An invalid answer to one question comes back as a `JevAnswer`
    with `error_type` set.

    `mocked` and `option_set_version` describe every call, so the gateway can
    write records even when a call fails.
    """

    mocked: bool
    option_set_version: str

    async def route(self, message: str) -> JevResult: ...


class ReplyChecker(Protocol):
    """The optional second Jev call (APP_JEV_REPLY_CHECK_ENABLED). A failure
    raises; the gateway records it and the customer is unaffected."""

    async def check_reply(
        self, message: str, reply: str, timeout: float | None = None
    ) -> ReplyCheckResult: ...


class MockJevRouter:
    """Stands in for the combined Jev call.

    Uses the best keyword match: its configured complexity for the route and its
    intent for the intent answer. Unmatched messages go to the standard route
    with intent `unknown`. Confidence and probabilities are demo heuristics, not
    calibrated, and the mock reports no tokens.
    """

    mocked = True

    def __init__(self, path: Path):
        self.intents = load_intents(path)
        self.intent_options = [item.intent for item in self.intents] + [UNKNOWN]
        # The mock chooses from its own intents, not the 78 labels.
        digest = hashlib.sha256(json.dumps(self.intent_options).encode()).hexdigest()
        self.option_set_version = f"mock-intents-{digest[:12]}"
        # Simulated faults, set through /v1/faults/jev; none by default.
        self.faults = JevFaultConfig()
        self.rng = random.Random()

    async def route(self, message: str) -> JevResult:
        # Draw only for a fault that is set, so runs without Jev faults keep
        # the same random sequence.
        faults = self.faults
        if faults.slow_rate and self.rng.random() < faults.slow_rate:
            # The gateway's Jev timeout cancels this wait.
            await asyncio.sleep(faults.slow_seconds)
        if faults.error_rate and self.rng.random() < faults.error_rate:
            raise ConnectionError("Simulated Jev outage")
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
            route_order=list(ROUTE_ORDER),
        )

    async def check_reply(
        self, message: str, reply: str, timeout: float | None = None
    ) -> ReplyCheckResult:
        """Mock second call: matches keywords in the message and in the reply.

        The route and intent come from the message and the reply together, so
        a reply that talks about another topic can pull the answer away from
        the first call. The reply fits when the reply alone points to the same
        intent as the message, or to none. Shares the first call's faults.
        """
        faults = self.faults
        if faults.slow_rate and self.rng.random() < faults.slow_rate:
            await asyncio.sleep(faults.slow_seconds)
        if faults.error_rate and self.rng.random() < faults.error_rate:
            raise ConnectionError("Simulated Jev outage")
        _, from_message = match_intent(self.intents, message)
        score, combined = match_intent(self.intents, f"{message} {reply}")
        _, from_reply = match_intent(self.intents, reply)
        if combined is None:
            route, intent, confidence = "standard", UNKNOWN, 0.5
        else:
            route, intent = combined.complexity, combined.intent
            confidence = round(min(0.95, 0.55 + score * 0.08), 2)
        other = "complex" if route == "standard" else "standard"
        wanted = from_message.intent if from_message else UNKNOWN
        said = from_reply.intent if from_reply else wanted
        fit = "fits" if said == wanted else "does_not_fit"
        not_fit = "does_not_fit" if fit == "fits" else "fits"
        return ReplyCheckResult(
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
            fit=JevAnswer(
                choice=fit,
                confidence=0.9,
                probabilities={fit: 0.9, not_fit: 0.1},
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
