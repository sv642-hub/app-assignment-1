"""Mock reply generation and bounded, process-local request history."""

import asyncio
import logging
import re
from collections import OrderedDict
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from app_assignment_1.config import Settings
from app_assignment_1.router import Router
from app_assignment_1.schemas import (
    BackendConfig,
    ChatResponse,
    PolicyCard,
    RoutingDecision,
    Trace,
)

logger = logging.getLogger(__name__)
MAX_REPLY_LENGTH = 4000


def check_reply(reply: str) -> list[str]:
    """Check generated text before returning or storing it."""
    errors = []
    if not reply.strip():
        errors.append("empty_reply")
    if len(reply) > MAX_REPLY_LENGTH:
        errors.append("reply_too_long")
    if re.search(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", reply):
        errors.append("unfilled_placeholder")
    return errors


class BackendUnavailable(Exception):
    """Neither configured mock backend is available."""


class Gateway:
    def __init__(self, settings: Settings, router: Router):
        self.router = router
        self.limit = settings.request_store_limit
        self.cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
            settings.policy_cards_path.read_text()
        )
        self.backends = TypeAdapter(list[BackendConfig]).validate_json(
            settings.backends_path.read_text()
        )
        expected = {("standard", "qwen3-1.7b"), ("complex", "qwen3-4b")}
        if (
            len(self.backends) != 2
            or {(b.route, b.name) for b in self.backends} != expected
        ):
            raise ValueError(
                "Configure exactly one standard 1.7b and one complex 4b backend"
            )
        self.requests: OrderedDict[UUID, ChatResponse] = OrderedDict()

    async def chat(self, message: str) -> ChatResponse:
        start = perf_counter()
        reason = None
        router_name: Literal["mock_jev", "rule_router"] = "mock_jev"
        try:
            decision = RoutingDecision.model_validate(await self.router.route(message))
        except Exception as exc:
            # Only isolate the replaceable router call. Cancellation still propagates.
            # Do not log input text, credentials or provider exception messages.
            logger.warning(
                "Router failed; using rule fallback (%s)", type(exc).__name__
            )
            decision = RoutingDecision(route="standard", intent="unknown", confidence=0)
            reason = "router_unavailable"
        jev_choice = decision.route if decision.intent != "unknown" else None
        if decision.intent == "unknown" or decision.intent not in self.cards:
            router_name = "rule_router"
            reason = reason or "no_matching_intent"
            decision = RoutingDecision(route="standard", intent="unknown", confidence=0)
            jev_choice = None
        backend = next(b for b in self.backends if b.route == decision.route)
        if not backend.available:
            available = next((b for b in self.backends if b.available), None)
            if available is None:
                raise BackendUnavailable
            backend = available
            reason = "selected_backend_unavailable"
        await asyncio.sleep(backend.latency_ms / 1000)
        if decision.intent == "unknown":
            facts: list[str] = []
            reply = (
                "I'm sorry, I could not match that message to a demo banking topic. "
                "Please rephrase it or try an example. This is a fictional bank demo."
            )
        else:
            card = self.cards[decision.intent]
            facts = card.facts
            reply = card.reply_template.format(facts=" ".join(facts))
        format_errors = check_reply(reply)
        if format_errors:
            # Keep malformed generated content out of the UI and stored response.
            reply = "Sorry, the demo could not produce a valid reply. Please try again."
            facts = []
        # The trace reports validation of the original generated reply.

        response = ChatResponse(
            id=uuid4(),
            intent=decision.intent,
            reply=reply,
            policy_facts=facts,
            trace=Trace(
                router=router_name,
                jev_choice=jev_choice,
                route=backend.route,
                model=backend.name,
                confidence=decision.confidence,
                latency_ms=round((perf_counter() - start) * 1000, 2),
                simulated_latency_ms=backend.latency_ms,
                format_check="failed" if format_errors else "passed",
                format_errors=format_errors,
                fallback_reason=reason,
            ),
        )
        self.requests[response.id] = response
        if len(self.requests) > self.limit:
            self.requests.popitem(last=False)
        return response
