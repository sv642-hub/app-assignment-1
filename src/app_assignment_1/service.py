"""Gateway flow; request history and interventions go to a Store."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from app_assignment_1.backends import Backend, MockBackend
from app_assignment_1.config import Settings
from app_assignment_1.intents import load_intents
from app_assignment_1.monitor import Controller
from app_assignment_1.router import Router, RuleRouter
from app_assignment_1.schemas import (
    BackendConfig,
    BackendReply,
    ChatResponse,
    ModelUsage,
    PolicyCard,
    RoutingDecision,
    Trace,
)
from app_assignment_1.store import MemoryStore, Store

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
    """No configured backend is available or able to answer."""


class Gateway:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        backends: Sequence[Backend] | None = None,
        clock: Callable[[], float] = time.monotonic,
        store: Store | None = None,
    ):
        self.router = router
        self.rule_router = RuleRouter(settings.rule_router_complex_min_words)
        self.jev_timeout = settings.jev_timeout_seconds
        self.store = store or MemoryStore(settings.request_store_limit)
        intents = load_intents(settings.intents_path)
        self.cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
            settings.policy_cards_path.read_text()
        )
        if set(self.cards) != {item.intent for item in intents}:
            raise ValueError("Policy cards must match the configured intents exactly")
        if backends is None:
            configs = TypeAdapter(list[BackendConfig]).validate_json(
                settings.backends_path.read_text()
            )
            backends = [MockBackend(c, intents, self.cards) for c in configs]
        self.backends = list(backends)
        self.usage = {
            b.config.name: ModelUsage(
                name=b.config.name, attempted_calls=0, completed_calls=0, failed_calls=0
            )
            for b in self.backends
        }
        expected = {("standard", "qwen3-1.7b"), ("complex", "qwen3-4b")}
        if (
            len(self.backends) != 2
            or {(b.config.route, b.config.name) for b in self.backends} != expected
        ):
            raise ValueError(
                "Configure exactly one standard 1.7b and one complex 4b backend"
            )
        self.controller = Controller(
            (b.config.name for b in self.backends), settings, clock
        )

    def usable(self, backend: Backend) -> bool:
        """Configured available and not disabled by the controller."""
        return backend.config.available and not self.controller.is_disabled(
            backend.config.name
        )

    def others_usable(self, backend: Backend) -> bool:
        return any(self.usable(b) for b in self.backends if b is not backend)

    async def save_interventions(self) -> None:
        """Move the controller's new interventions to the store."""
        items, self.controller.unsaved = self.controller.unsaved, []
        if not items:
            return
        try:
            await self.store.save_interventions(items)
        except Exception as exc:
            # Keep them for the next attempt; the controller itself is unaffected.
            self.controller.unsaved = items + self.controller.unsaved
            logger.warning("Could not save interventions (%s)", type(exc).__name__)

    async def chat(self, message: str) -> ChatResponse:
        try:
            response = await self._chat(message)
        finally:
            # Interventions also happen on requests that end in a 503.
            await self.save_interventions()
        try:
            await self.store.save_request(response)
        except Exception as exc:
            # The customer still gets the reply; it just cannot be looked up later.
            logger.warning("Could not save request (%s)", type(exc).__name__)
        return response

    async def _chat(self, message: str) -> ChatResponse:
        start = perf_counter()
        reasons = []
        router_name: Literal["mock_jev", "rule_router"] = "mock_jev"
        try:
            decision = RoutingDecision.model_validate(
                await asyncio.wait_for(self.router.route(message), self.jev_timeout)
            )
            jev_choice = decision.route
        except Exception as exc:
            # Only isolate the replaceable router call. Cancellation still propagates.
            # Do not log input text, credentials or provider exception messages.
            logger.warning(
                "Jev routing failed; using rule router (%s)", type(exc).__name__
            )
            timed_out = isinstance(exc, TimeoutError)
            reasons.append("jev_timeout" if timed_out else "jev_error")
            decision = await self.rule_router.route(message)
            router_name = "rule_router"
            jev_choice = None
        selected = next(b for b in self.backends if b.config.route == decision.route)
        action = None
        for backend in (selected, *(b for b in self.backends if b is not selected)):
            if not self.usable(backend):
                if backend is selected:
                    reasons.append(
                        "selected_backend_disabled"
                        if backend.config.available
                        else "selected_backend_unavailable"
                    )
                continue
            usage = self.usage[backend.config.name]
            usage.attempted_calls += 1
            try:
                generated = BackendReply.model_validate(await backend.generate(message))
                usage.completed_calls += 1
                break
            except Exception as exc:
                usage.failed_calls += 1
                # A reply that fails BackendReply validation also counts as a call
                # error. Cancellation still propagates. Do not log input text or
                # provider exception messages.
                logger.warning(
                    "Backend %s failed (%s)", backend.config.name, type(exc).__name__
                )
                action = self.controller.record_error(
                    backend.config.name, can_disable=self.others_usable(backend)
                )
                if backend is selected:
                    reasons.append("selected_backend_error")
        else:
            raise BackendUnavailable
        intent, reply, facts = generated.intent, generated.reply, generated.policy_facts
        format_errors = check_reply(reply)
        if intent != "unknown" and intent not in self.cards:
            format_errors.append("unknown_label")
        if format_errors:
            # Keep malformed generated content out of the UI and stored response.
            intent = "unknown"
            reply = "Sorry, the demo could not produce a valid reply. Please try again."
            facts = []
        # The trace reports validation of the original generated reply.
        action = (
            self.controller.record(
                backend.config.name,
                bool(format_errors),
                can_disable=self.others_usable(backend),
            )
            or action
        )

        response = ChatResponse(
            id=uuid4(),
            intent=intent,
            reply=reply,
            policy_facts=facts,
            trace=Trace(
                router=router_name,
                jev_choice=jev_choice,
                route=backend.config.route,
                model=backend.config.name,
                reply_source=(
                    "format_fallback"
                    if format_errors
                    else "no_match"
                    if intent == "unknown"
                    else "policy_template"
                ),
                confidence=decision.confidence,
                latency_ms=round((perf_counter() - start) * 1000, 2),
                simulated_latency_ms=backend.config.latency_ms,
                format_check="failed" if format_errors else "passed",
                format_errors=format_errors,
                fallback_reason=", ".join(reasons) or None,
                controller_action=action,
            ),
        )
        return response
