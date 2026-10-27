"""Mock Jev routes, mock backend intents and the rule router, for all intents."""

import asyncio
import json
from pathlib import Path

import pytest

from app_assignment_1.backends import MockBackend
from app_assignment_1.intents import load_intents
from app_assignment_1.router import MockJevRouter, RuleRouter
from app_assignment_1.schemas import BackendConfig, PolicyCard

INTENTS = Path("config/intents.json")


def backend() -> MockBackend:
    cards = {
        name: PolicyCard.model_validate(card)
        for name, card in json.loads(
            Path("config/policy_cards.json").read_text()
        ).items()
    }
    config = BackendConfig(name="qwen3-1.7b", route="standard", latency_ms=0)
    return MockBackend(config, load_intents(INTENTS), cards)


def classify(message: str) -> str:
    return asyncio.run(backend().generate(message)).intent


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
def test_jev_route_and_backend_intent(intent, phrase, route):
    decision = asyncio.run(MockJevRouter(INTENTS).route(phrase))
    assert decision.route == route
    assert decision.confidence is not None and 0 < decision.confidence <= 1
    assert classify(phrase) == intent


def test_jev_returns_route_only():
    decision = asyncio.run(MockJevRouter(INTENTS).route("I lost my card"))
    assert set(decision.model_dump()) == {"route", "confidence"}


def test_unmatched_message_routes_standard_and_classifies_unknown():
    decision = asyncio.run(MockJevRouter(INTENTS).route("weather on Mars"))
    assert decision.route == "standard"
    reply = asyncio.run(backend().generate("weather on Mars"))
    assert reply.intent == "unknown"
    assert reply.policy_facts == []
    assert "rephrase" in reply.reply


def test_no_substring_match():
    assert classify("exchange ratesetter") == "unknown"


@pytest.mark.parametrize(
    "words,route", [(3, "standard"), (19, "standard"), (20, "complex")]
)
def test_rule_router_uses_message_length(words, route):
    decision = asyncio.run(RuleRouter(20).route(" ".join(["card"] * words)))
    assert decision.route == route
    assert decision.confidence is None


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
    assert classify(phrase) == intent
