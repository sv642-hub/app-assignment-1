"""MockJevRouter tests: intent, route, and confidence from config/intents.json."""

import asyncio

import pytest

from smart_gateway.catalog import Catalog, IntentSpec
from smart_gateway.router import UNKNOWN_INTENT, IntentRouter, MockJevRouter


@pytest.fixture
def router(catalog: Catalog) -> MockJevRouter:
    return MockJevRouter(catalog.intents)


@pytest.mark.parametrize(
    ("message", "intent", "route"),
    [
        ("My new card still hasn't arrived", "card_arrival", "standard"),
        ("I lost my card on the train", "lost_or_stolen_card", "complex"),
        (
            "The recipient hasn't received my transfer",
            "transfer_not_received_by_recipient",
            "complex",
        ),
        ("What exchange rate do you use?", "exchange_rate", "standard"),
        ("My top up failed twice", "top_up_failed", "standard"),
        ("Why is my transfer still pending?", "pending_transfer", "standard"),
        (
            "Why was I charged a fee for my card payment?",
            "card_payment_fee_charged",
            "complex",
        ),
        ("How do I verify my identity?", "verify_my_identity", "standard"),
        ("My refund is not showing up", "refund_not_showing_up", "complex"),
        (
            "The ATM gave me the wrong amount of cash",
            "wrong_amount_of_cash_received",
            "complex",
        ),
    ],
)
def test_routes_each_intent(
    router: MockJevRouter, message: str, intent: str, route: str
) -> None:
    decision = asyncio.run(router.route(message))

    assert decision.intent == intent
    assert decision.route == route
    assert 0 < decision.confidence <= 1


def test_intents_file_has_about_ten_intents(catalog: Catalog) -> None:
    assert len(catalog.intents) == 10


def test_unknown_message_has_unknown_intent(router: MockJevRouter) -> None:
    decision = asyncio.run(router.route("Can you recommend a pizza place?"))

    assert decision.intent == UNKNOWN_INTENT
    assert decision.route == "standard"
    assert decision.confidence == 0


def test_matching_is_case_and_apostrophe_insensitive(router: MockJevRouter) -> None:
    decision = asyncio.run(router.route("MY CARD HASN’T ARRIVED"))

    assert decision.intent == "card_arrival"


def test_keywords_match_whole_words_only(router: MockJevRouter) -> None:
    # "refund" inside "unrefundedness" must not count as the keyword.
    decision = asyncio.run(router.route("unrefundedness"))

    assert decision.intent == UNKNOWN_INTENT


def test_confidence_is_share_of_score() -> None:
    router = MockJevRouter(
        [
            IntentSpec(name="a", complexity="standard", keywords=["card"]),
            IntentSpec(name="b", complexity="complex", keywords=["lost card"]),
        ]
    )

    decision = asyncio.run(router.route("lost card"))

    # "lost card" scores 2 for b; "card" scores 1 for a.
    assert decision.intent == "b"
    assert decision.route == "complex"
    assert decision.confidence == pytest.approx(2 / 3, abs=1e-3)


def test_mock_router_satisfies_interface(router: MockJevRouter) -> None:
    assert isinstance(router, IntentRouter)
