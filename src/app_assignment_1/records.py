"""Records the gateway writes for the semantic monitor.

Only the Jev assessment is defined so far. The backend response record waits on
the open points in PR 45 (see docs/JEV_QWEN_INTEGRATION_PLAN.md). Nothing in the
gateway writes these records yet; they will be saved through `Store`.
"""

import math
from collections.abc import Collection, Sequence
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app_assignment_1.schemas import StrictModel

ROUTE_OPTIONS = frozenset({"standard", "complex"})
# Jev's probabilities are rounded, so the total is only close to 1.
PROBABILITY_TOTAL_TOLERANCE = 0.05
# Two options can share the top probability. Allow for rounding when comparing.
TIE_TOLERANCE = 1e-9


class JevAssessment(StrictModel):
    """One answer to one question in a Jev call.

    Each message gets one combined Jev call with two Choice questions, `route`
    and `intent`, so a normal request writes two records with the same
    `call_id`. Latency and tokens belong to the whole call: count them once per
    `call_id`. The record never stores the customer's message.
    """

    request_id: UUID
    # Created by the gateway for each Jev HTTP call. Both answers share it.
    call_id: UUID
    question: Literal["route", "intent"]
    status: Literal["ok", "timeout", "error", "skipped"]
    finished_at: AwareDatetime
    mocked: bool
    # Which labels Jev chose from. Answers are only comparable within one set.
    option_set_version: str = Field(min_length=1)
    # Measured by the gateway for the whole call.
    latency_ms: float | None = Field(default=None, ge=0)
    # A short code such as rate_limited, never provider message text.
    error_type: str | None = Field(default=None, min_length=1)
    # Why Jev was not called. error_type stays empty, because nothing failed.
    skip_reason: Literal["circuit_open"] | None = None
    jev_model: str | None = Field(default=None, min_length=1)  # e.g. jev-1.14.0
    provider_request_id: str | None = None  # x-typesafe-request-id, if present
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    choice: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] | None = None

    @model_validator(mode="after")
    def check_status(self) -> "JevAssessment":
        if (self.error_type is not None) != (self.status in ("timeout", "error")):
            raise ValueError("error_type is set exactly when the call failed")
        if (self.skip_reason is not None) != (self.status == "skipped"):
            raise ValueError("skip_reason is set exactly when Jev was skipped")
        answer = (self.choice, self.confidence, self.probabilities)
        if self.status != "ok":
            if any(value is not None for value in answer):
                raise ValueError("Only an ok answer has a choice")
            if self.status == "skipped":
                call_details = (
                    self.latency_ms,
                    self.jev_model,
                    self.provider_request_id,
                    self.input_tokens,
                    self.output_tokens,
                )
                if any(value is not None for value in call_details):
                    raise ValueError("A skipped question made no call")
            return self
        if any(value is None for value in answer):
            raise ValueError(
                "An ok answer needs a choice, confidence and probabilities"
            )
        if (
            self.jev_model is None
            or self.input_tokens is None
            or self.output_tokens is None
        ):
            raise ValueError("An ok answer needs the model and token counts")
        assert self.choice is not None and self.probabilities is not None
        check_probabilities(self.choice, self.probabilities)
        if self.question == "route":
            check_options(self, ROUTE_OPTIONS)
        return self


def check_probabilities(choice: str, probabilities: dict[str, float]) -> None:
    """Raise unless the probabilities are valid and the choice is a top option."""
    values = list(probabilities.values())
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise ValueError("Each probability must be a number from 0 to 1")
    if abs(sum(values) - 1) > PROBABILITY_TOTAL_TOLERANCE:
        raise ValueError("Probabilities must add up to about 1")
    if choice not in probabilities:
        raise ValueError("The choice must be one of the options")
    if probabilities[choice] < max(values) - TIE_TOLERANCE:
        raise ValueError("The choice must have the highest probability")


def check_options(record: JevAssessment, options: Collection[str]) -> None:
    """Raise unless an ok answer's probabilities cover exactly the options asked.

    The intent options come from the label file, which the record does not
    know, so the caller passes them in.
    """
    if record.status != "ok" or record.probabilities is None:
        return
    if set(record.probabilities) != set(options):
        raise ValueError(f"The {record.question} answer must cover the options asked")


CALL_FIELDS = (
    "request_id",
    "call_id",
    "mocked",
    "latency_ms",
    "jev_model",
    "provider_request_id",
    "input_tokens",
    "output_tokens",
)


def check_call(records: Sequence[JevAssessment]) -> None:
    """Raise unless the records are one route and one intent answer from one call.

    The gateway copies the call's details into both records, even when only one
    answer was valid, so the two must agree on them. Skipping applies to the
    whole call.
    """
    if sorted(record.question for record in records) != ["intent", "route"]:
        raise ValueError("A Jev call has one route and one intent answer")
    first, second = records
    for field in CALL_FIELDS:
        if getattr(first, field) != getattr(second, field):
            raise ValueError(f"Both answers from one call must share {field}")
    if (first.status == "skipped") != (second.status == "skipped"):
        raise ValueError("Jev is skipped for the whole call or not at all")
