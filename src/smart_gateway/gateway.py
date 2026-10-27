"""The request pipeline: route, answer from a policy card, check, record."""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from time import perf_counter

from smart_gateway.backends import MockBackend
from smart_gateway.catalog import Catalog, Route
from smart_gateway.router import UNKNOWN_INTENT, IntentRouter, RouteDecision
from smart_gateway.schemas import (
    ChatResponse,
    FallbackReason,
    FormatCheck,
    PolicyCardRef,
    Trace,
)

logger = logging.getLogger(__name__)

RULE_ROUTER = "rule-router"
MAX_REPLY_CHARS = 600


def check_format(reply: str) -> FormatCheck:
    """Pass a reply that is non-empty, short, and has no unfilled template."""
    ok = bool(reply.strip()) and len(reply) <= MAX_REPLY_CHARS
    return "pass" if ok and "{" not in reply and "}" not in reply else "fail"


class RequestStore:
    """In-memory record of recent responses; the oldest are dropped first."""

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._items: OrderedDict[str, ChatResponse] = OrderedDict()

    def add(self, response: ChatResponse) -> None:
        self._items[response.request_id] = response
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)

    def get(self, request_id: str) -> ChatResponse | None:
        return self._items.get(request_id)


class Gateway:
    """Routes a message, answers it with the chosen backend, and records it.

    A message with no matching intent, or a router that raises, takes the rule
    router path: intent "unknown", a polite fixed reply, and no backend call.
    """

    def __init__(
        self, router: IntentRouter, catalog: Catalog, store: RequestStore
    ) -> None:
        self.router = router
        self.catalog = catalog
        self.store = store
        self.backends: dict[Route, MockBackend] = {
            route: MockBackend(spec) for route, spec in catalog.backends.items()
        }

    async def chat(self, message: str) -> ChatResponse:
        start = perf_counter()
        fallback_reason: FallbackReason | None = None
        try:
            decision = await self.router.route(message)
        except Exception:
            logger.exception("Router %r failed; using the rule router.", self.router)
            decision = RouteDecision(
                route="standard", intent=UNKNOWN_INTENT, confidence=0
            )
            fallback_reason = "router_error"

        card = self.catalog.cards.get(decision.intent)
        if card is None:
            fallback_reason = fallback_reason or "no_intent_match"
            router_name, model, reply = RULE_ROUTER, None, self.catalog.fallback_reply
            intent, card_ref = UNKNOWN_INTENT, None
        else:
            backend = self.backends[decision.route]
            router_name, model = self.router.name, backend.name
            reply = await backend.generate(message, card)
            intent = decision.intent
            card_ref = PolicyCardRef(
                intent=card.intent, title=card.title, facts=card.facts
            )

        response = ChatResponse(
            request_id=uuid.uuid4().hex,
            created_at=datetime.now(UTC),
            message=message,
            intent=intent,
            reply=reply,
            policy_card=card_ref,
            trace=Trace(
                router=router_name,
                route=decision.route,
                confidence=decision.confidence,
                model=model,
                latency_ms=round((perf_counter() - start) * 1000, 1),
                format_check=check_format(reply),
                fallback_reason=fallback_reason,
            ),
        )
        self.store.add(response)
        return response
