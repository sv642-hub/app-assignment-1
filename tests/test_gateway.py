"""Gateway pipeline tests: backend choice, grounding, fallback, and the store."""

import asyncio

from smart_gateway.catalog import Catalog
from smart_gateway.gateway import Gateway, RequestStore, check_format
from smart_gateway.router import MockJevRouter, RouteDecision


def make_gateway(catalog: Catalog, store_size: int = 100) -> Gateway:
    return Gateway(MockJevRouter(catalog.intents), catalog, RequestStore(store_size))


class FailingRouter:
    name = "broken-jev"

    async def route(self, message: str) -> RouteDecision:
        raise TimeoutError("Jev did not answer")


def test_standard_route_uses_small_model(fast_catalog: Catalog) -> None:
    response = asyncio.run(make_gateway(fast_catalog).chat("What exchange rate?"))

    assert response.intent == "exchange_rate"
    assert response.trace.router == "mock-jev"
    assert response.trace.route == "standard"
    assert response.trace.model == "qwen3-1.7b"
    assert response.trace.fallback_reason is None


def test_complex_route_uses_large_model(fast_catalog: Catalog) -> None:
    response = asyncio.run(make_gateway(fast_catalog).chat("My card was stolen"))

    assert response.trace.route == "complex"
    assert response.trace.model == "qwen3-4b"


def test_reply_is_grounded_in_policy_card(fast_catalog: Catalog) -> None:
    response = asyncio.run(make_gateway(fast_catalog).chat("I lost my card"))

    card = fast_catalog.cards["lost_or_stolen_card"]
    assert response.policy_card is not None
    assert response.policy_card.facts == card.facts
    for fact in card.facts:
        assert fact in response.reply
    assert response.trace.format_check == "pass"


def test_unknown_message_falls_back_to_rule_router(fast_catalog: Catalog) -> None:
    response = asyncio.run(make_gateway(fast_catalog).chat("Tell me a joke"))

    assert response.intent == "unknown"
    assert response.reply == fast_catalog.fallback_reply
    assert response.policy_card is None
    assert response.trace.router == "rule-router"
    assert response.trace.model is None
    assert response.trace.fallback_reason == "no_intent_match"
    assert response.trace.format_check == "pass"


def test_router_error_falls_back_to_rule_router(fast_catalog: Catalog) -> None:
    gateway = Gateway(FailingRouter(), fast_catalog, RequestStore(10))

    response = asyncio.run(gateway.chat("I lost my card"))

    assert response.intent == "unknown"
    assert response.trace.router == "rule-router"
    assert response.trace.fallback_reason == "router_error"


def test_latency_includes_simulated_backend_time(catalog: Catalog) -> None:
    response = asyncio.run(make_gateway(catalog).chat("What exchange rate?"))

    assert response.trace.latency_ms >= catalog.backends["standard"].latency_ms


def test_store_keeps_responses_and_drops_oldest(fast_catalog: Catalog) -> None:
    gateway = make_gateway(fast_catalog, store_size=2)

    first, second, third = (
        asyncio.run(gateway.chat(m)) for m in ["lost card", "refund", "kyc"]
    )

    assert gateway.store.get(first.request_id) is None
    assert gateway.store.get(second.request_id) == second
    assert gateway.store.get(third.request_id) == third


def test_format_check() -> None:
    assert check_format("A short reply.") == "pass"
    assert check_format("   ") == "fail"
    assert check_format("Hi {name}") == "fail"
    assert check_format("x" * 601) == "fail"
