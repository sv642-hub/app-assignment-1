"""All configured intents plus casing and word-boundary behavior."""

import asyncio
from pathlib import Path

import pytest

from app_assignment_1.router import MockJevRouter


@pytest.mark.parametrize(
    "intent,phrase,route",
    [
        ("card_arrival", "MY CARD HAS NOT ARRIVED!", "standard"),
        ("lost_or_stolen_card", "I lost my card", "complex"),
        (
            "transfer_not_received_by_recipient",
            "My transfer has not arrived",
            "complex",
        ),
        ("exchange_rate", "What is the exchange rate?", "standard"),
        ("top_up_failed", "My top-up failed", "complex"),
        ("pending_transfer", "The transfer is pending", "standard"),
        ("card_payment_fee_charged", "I was charged a fee", "complex"),
        ("verify_my_identity", "How do I verify my identity?", "standard"),
        ("refund_not_showing_up", "My refund is missing: missing refund", "complex"),
        ("wrong_amount_of_cash_received", "The ATM gave me less", "complex"),
    ],
)
def test_routing(intent, phrase, route):
    result = asyncio.run(MockJevRouter(Path("config/intents.json")).route(phrase))
    assert result.intent == intent
    assert result.route == route
    assert 0 < result.confidence <= 1


def test_no_substring_match():
    result = asyncio.run(
        MockJevRouter(Path("config/intents.json")).route("exchange ratesetter")
    )
    assert result.intent == "unknown"


@pytest.mark.parametrize(
    "phrase,intent",
    [
        ("What are your exchange rates?", "exchange_rate"),
        ("Someone stole my card", "lost_or_stolen_card"),
        ("Where is my refund?", "refund_not_showing_up"),
        ("Why is my transfer still pending?", "pending_transfer"),
        ("When will my card arrive?", "card_arrival"),
        ("My transfer hasn't arrived", "transfer_not_received_by_recipient"),
        ("My top-up was declined", "top_up_failed"),
        ("There is a fee on my card payment", "card_payment_fee_charged"),
        ("How do I verify myself?", "verify_my_identity"),
        ("The ATM gave me the wrong amount", "wrong_amount_of_cash_received"),
    ],
)
def test_plain_customer_phrasings(phrase, intent):
    result = asyncio.run(MockJevRouter(Path("config/intents.json")).route(phrase))
    assert result.intent == intent
