"""Jev assessment records: status rules, answers and the combined call."""

import math
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app_assignment_1.records import (
    JevAssessment,
    check_call,
    check_options,
)

REQUEST_ID = uuid4()
CALL_ID = uuid4()
INTENTS = {"card_arrival", "lost_or_stolen_card", "unknown"}


def call_details(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "call_id": CALL_ID,
        "finished_at": datetime(2026, 11, 9, tzinfo=UTC),
        "mocked": False,
        "option_set_version": "labels-0123456789ab",
        "latency_ms": 840.0,
        "jev_model": "jev-1.14.0",
        "provider_request_id": "req_123",
        "input_tokens": 900,
        "output_tokens": 12,
    }
    values.update(overrides)
    return values


def route_answer(**overrides: Any) -> dict[str, Any]:
    values = call_details(
        question="route",
        status="ok",
        choice="complex",
        confidence=0.6,
        probabilities={"standard": 0.3, "complex": 0.7},
    )
    values.update(overrides)
    return values


def intent_answer(**overrides: Any) -> dict[str, Any]:
    values = call_details(
        question="intent",
        status="ok",
        choice="card_arrival",
        confidence=0.8,
        probabilities={
            "card_arrival": 0.9,
            "lost_or_stolen_card": 0.05,
            "unknown": 0.05,
        },
    )
    values.update(overrides)
    return values


def failed(question: str, status: str, **overrides: Any) -> dict[str, Any]:
    values = call_details(
        question=question,
        status=status,
        error_type="timeout" if status == "timeout" else "http_500",
        jev_model=None,
        provider_request_id=None,
        input_tokens=None,
        output_tokens=None,
    )
    values.update(overrides)
    return values


def skipped(question: str, **overrides: Any) -> dict[str, Any]:
    values = call_details(
        question=question,
        status="skipped",
        skip_reason="circuit_open",
        latency_ms=None,
        jev_model=None,
        provider_request_id=None,
        input_tokens=None,
        output_tokens=None,
    )
    values.update(overrides)
    return values


def invalid(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        JevAssessment(**values)


@pytest.mark.parametrize(
    "values",
    [
        route_answer(),
        intent_answer(),
        failed("route", "timeout"),
        failed("intent", "error"),
        skipped("route"),
        skipped("intent"),
        # A tie at the top is allowed.
        route_answer(
            choice="standard", probabilities={"standard": 0.5, "complex": 0.5}
        ),
        # Rounded probabilities only need to add up to about 1.
        route_answer(probabilities={"standard": 0.32, "complex": 0.71}),
        # An invalid route answer from a call that returned keeps the call details.
        failed("route", "error", jev_model="jev-1.14.0", input_tokens=900),
    ],
)
def test_valid_records(values: dict[str, Any]) -> None:
    JevAssessment(**values)


def test_skipped_route_is_allowed() -> None:
    record = JevAssessment(**skipped("route"))
    assert record.skip_reason == "circuit_open"
    assert record.error_type is None


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (route_answer(error_type="timeout"), "error_type is set exactly"),
        (failed("route", "timeout", error_type=None), "error_type is set exactly"),
        (skipped("route", error_type="circuit_open"), "error_type is set exactly"),
        (skipped("route", skip_reason=None), "skip_reason is set exactly"),
        (route_answer(skip_reason="circuit_open"), "skip_reason is set exactly"),
        (failed("route", "error", skip_reason="circuit_open"), "skip_reason is set"),
        (skipped("intent", skip_reason="sampled_out"), "skip_reason"),
        (failed("intent", "timeout", choice="unknown"), "Only an ok answer"),
        (skipped("intent", confidence=0.5), "Only an ok answer"),
        (skipped("intent", latency_ms=10.0), "A skipped question made no call"),
        (skipped("route", input_tokens=900), "A skipped question made no call"),
        (route_answer(choice=None), "needs a choice"),
        (intent_answer(probabilities=None), "needs a choice"),
        (route_answer(jev_model=None), "model and token counts"),
        (intent_answer(output_tokens=None), "model and token counts"),
        (
            route_answer(probabilities={"standard": 0.3, "complex": math.nan}),
            "from 0 to 1",
        ),
        (
            route_answer(probabilities={"standard": -0.1, "complex": 1.1}),
            "from 0 to 1",
        ),
        (
            route_answer(probabilities={"standard": 0.3, "complex": 0.3}),
            "add up to about 1",
        ),
        (
            route_answer(
                choice="unknown", probabilities={"standard": 0.3, "complex": 0.7}
            ),
            "one of the options",
        ),
        (route_answer(choice="standard"), "highest probability"),
        (
            route_answer(probabilities={"standard": 0.3, "complex": 0.6, "other": 0.1}),
            "route answer must cover",
        ),
        (route_answer(question="routing"), "question"),
        (route_answer(finished_at=datetime(2026, 11, 9)), "timezone"),
        (route_answer(confidence=1.5), "confidence"),
        (route_answer(latency_ms=-1), "latency_ms"),
        (route_answer(option_set_version=""), "option_set_version"),
        (failed("route", "error", error_type=""), "error_type"),
        (route_answer(message="where is my card"), "Extra inputs"),
    ],
)
def test_invalid_records(values: dict[str, Any], message: str) -> None:
    invalid(values, message)


def test_intent_options_are_checked_against_the_options_asked() -> None:
    record = JevAssessment(**intent_answer())
    check_options(record, INTENTS)
    with pytest.raises(ValueError, match="intent answer must cover"):
        check_options(record, INTENTS | {"card_linking"})
    with pytest.raises(ValueError, match="intent answer must cover"):
        check_options(record, {"card_arrival", "unknown"})


def test_options_are_not_checked_without_an_answer() -> None:
    check_options(JevAssessment(**failed("intent", "timeout")), INTENTS)


@pytest.mark.parametrize(
    "pair",
    [
        (route_answer(), intent_answer()),
        (intent_answer(), route_answer()),
        (failed("route", "timeout"), failed("intent", "timeout")),
        (skipped("route"), skipped("intent")),
        # Invalid route answer, valid intent answer.
        (failed("route", "error", **call_details()), intent_answer()),
        # Valid route answer, invalid intent answer.
        (route_answer(), failed("intent", "error", **call_details())),
    ],
)
def test_one_call_writes_a_matching_pair(
    pair: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    check_call([JevAssessment(**values) for values in pair])


@pytest.mark.parametrize(
    ("pair", "message"),
    [
        ((route_answer(), route_answer()), "one route and one intent"),
        ((route_answer(),), "one route and one intent"),
        ((route_answer(), intent_answer(call_id=uuid4())), "share call_id"),
        ((route_answer(), intent_answer(request_id=uuid4())), "share request_id"),
        ((route_answer(), intent_answer(latency_ms=10.0)), "share latency_ms"),
        ((route_answer(), intent_answer(input_tokens=1)), "share input_tokens"),
        ((route_answer(), intent_answer(mocked=True)), "share mocked"),
        (
            (skipped("route"), failed("intent", "timeout", latency_ms=None)),
            "whole call",
        ),
    ],
)
def test_mismatched_pairs_are_rejected(
    pair: tuple[dict[str, Any], ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check_call([JevAssessment(**values) for values in pair])


def test_records_round_trip_through_json() -> None:
    record = JevAssessment(**skipped("intent"))
    assert JevAssessment.model_validate_json(record.model_dump_json()) == record
