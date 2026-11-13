"""Gateway flow; requests, their Jev assessments and interventions go to a Store."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from app_assignment_1.backends import Backend, MockBackend
from app_assignment_1.breaker import CircuitBreaker
from app_assignment_1.config import Settings
from app_assignment_1.intents import load_intents
from app_assignment_1.labels import UNKNOWN, load_labels
from app_assignment_1.metrics import GatewayMetrics
from app_assignment_1.monitor import Controller
from app_assignment_1.records import JevAssessment, check_call
from app_assignment_1.release import (
    Release,
    ReleaseChange,
    ReleaseSource,
    VersionConfig,
    load_versions,
    pick_version,
)
from app_assignment_1.router import JevAnswer, JevResult, Router, RuleRouter
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
QUESTIONS: tuple[Literal["route", "intent"], ...] = ("route", "intent")
MAX_REPLY_LENGTH = 4000
# For a recognized intent that has no policy card. The backend sends an empty
# reply for these; the gateway never shows generated text for them.
UNCARDED_REPLY = (
    "I recognized your banking question, but this demo does not cover that "
    "topic yet, so I can't give policy details. This is a fictional bank demo."
)


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


class ReleaseNotSaved(Exception):
    """The store did not save a release change, so it was not applied."""


class ReleaseOutcomeUnknown(Exception):
    """A release save failed and the store could not be read to check it. The
    change may have been saved; the gateway reloads on the next chance."""


def _jev_error_type(exc: Exception) -> str:
    """A short code for a failed Jev call. Never the exception's message."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    code = getattr(exc, "error_type", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, ValidationError):
        return "invalid_result"
    return "router_error"


def _assessment(
    call: dict[str, Any], question: Literal["route", "intent"], answer: JevAnswer
) -> JevAssessment:
    if answer.error_type is not None:
        return JevAssessment(
            **call, question=question, status="error", error_type=answer.error_type
        )
    return JevAssessment(
        **call,
        question=question,
        status="ok",
        choice=answer.choice,
        confidence=answer.confidence,
        probabilities=answer.probabilities,
    )


class Gateway:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        backends: Sequence[Backend] | None = None,
        clock: Callable[[], float] = time.monotonic,
        store: Store | None = None,
    ):
        if settings.provider_mode == "real":
            raise ValueError(
                "APP_PROVIDER_MODE=real is not built yet; the Jev and Qwen clients "
                "come in later PRs"
            )
        self.router = router
        # Test doubles may leave these out; both real routers set them.
        self.jev_mocked: bool = getattr(router, "mocked", True)
        self.jev_option_set_version: str = getattr(
            router, "option_set_version", "unspecified"
        )
        self.jev_breaker = CircuitBreaker(
            settings.jev_breaker_failures, settings.jev_breaker_cooldown_seconds, clock
        )
        self.rule_router = RuleRouter(settings.rule_router_complex_min_words)
        self.jev_timeout = settings.jev_timeout_seconds
        self.store = store or MemoryStore(settings.request_store_limit)
        # Each store write waits at most this long, so a database outage
        # delays a reply by seconds, not by the pool's 30-second default.
        self.store_timeout = settings.store_timeout_seconds
        self.save_lock = asyncio.Lock()
        intents = load_intents(settings.intents_path)
        self.cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
            settings.policy_cards_path.read_text()
        )
        if set(self.cards) != {item.intent for item in intents}:
            raise ValueError("Policy cards must match the configured intents exactly")
        self.labels = load_labels(settings.labels_path)
        if not set(self.cards) <= self.labels.names:
            raise ValueError("Every intent with a policy card must be a label")
        # One backend pair per config version (release.py).
        self.version_backends: dict[str, list[Backend]] = {}
        if backends is None:
            self.versions = load_versions(settings.versions_path)
            for version in self.versions:
                path = version.backends_path or settings.backends_path
                configs = TypeAdapter(list[BackendConfig]).validate_json(
                    path.read_text()
                )
                self.version_backends[version.id] = [
                    MockBackend(c, intents, self.cards) for c in configs
                ]
        else:
            # Backends passed in (tests) are the only version.
            self.versions = [
                VersionConfig(
                    id=settings.stable_version,
                    description="Backends passed to the gateway.",
                )
            ]
            self.version_backends[settings.stable_version] = list(backends)
        expected = {("standard", "qwen3-1.7b"), ("complex", "qwen3-4b")}
        for pair in self.version_backends.values():
            if (
                len(pair) != 2
                or {(b.config.route, b.config.name) for b in pair} != expected
            ):
                raise ValueError(
                    "Configure exactly one standard 1.7b and one complex 4b backend"
                )
        # Usage, the monitor and the controller count per model name, across
        # versions: a failing canary counts against its model for everyone.
        # Every version has the same two model names (checked above).
        pair = next(iter(self.version_backends.values()))
        self.usage = {
            b.config.name: ModelUsage(
                name=b.config.name, attempted_calls=0, completed_calls=0, failed_calls=0
            )
            for b in pair
        }
        self.controller = Controller((b.config.name for b in pair), settings, clock)
        self.release = self.check_release(
            ReleaseChange(
                stable=settings.stable_version,
                canary=settings.canary_version,
                canary_percent=settings.canary_percent,
                reason="Initial release from settings",
            )
        )
        self.release_source: ReleaseSource = "settings"
        self.release_lock = asyncio.Lock()
        self.metrics = GatewayMetrics(self)

    @property
    def backends(self) -> list[Backend]:
        """The stable version's backends: what /v1/health reports."""
        return self.version_backends[self.release.stable]

    def check_versions(self, change: ReleaseChange) -> None:
        """Raise ValueError if the change names a version not configured."""
        for version in (change.stable, change.canary):
            if version is not None and version not in self.version_backends:
                raise ValueError(f"Unknown version: {version}")

    def check_release(self, change: ReleaseChange) -> Release:
        """A release for `change`, in effect from now."""
        self.check_versions(change)
        return Release(**change.model_dump(), changed_at=datetime.now(UTC))

    async def _latest_saved_release(self) -> list[Release]:
        return await asyncio.wait_for(self.store.recent_releases(1), self.store_timeout)

    async def load_release(self) -> None:
        """Use the store's latest release, if it has one this gateway can run.

        Call after the store is open. If the store cannot be read, the release
        from settings stays in effect and `release_source` says so, so the
        operator can see that a saved release (a rollback, say) may be
        waiting. `ensure_release_loaded` retries.
        """
        try:
            saved = await self._latest_saved_release()
        except Exception as exc:
            logger.warning("Could not load the release (%s)", type(exc).__name__)
            self.release_source = "settings_store_unreadable"
            return
        if not saved:
            self.release_source = "settings"
            return
        try:
            self.check_versions(saved[0])
        except ValueError:
            logger.warning(
                "The saved release names a version that is not configured; "
                "using the release from settings"
            )
            self.release_source = "settings"
            return
        self.release = saved[0]
        self.release_source = "store"

    async def ensure_release_loaded(self) -> None:
        """Retry the load if the store could not be read before."""
        if self.release_source != "settings_store_unreadable":
            return
        async with self.release_lock:
            if self.release_source == "settings_store_unreadable":
                await self.load_release()

    async def set_release(self, change: ReleaseChange) -> Release:
        """Save a release change, then apply it. A change that is not saved
        is not applied, so a restart cannot undo it silently.

        A failed save may still have been committed (a timeout after the
        database wrote the row), so the latest saved release is read back
        before reporting failure.
        """
        async with self.release_lock:
            # Stamped inside the lock, so saved order matches changed_at.
            release = self.check_release(change)
            try:
                await asyncio.wait_for(
                    self.store.save_release(release), self.store_timeout
                )
            except Exception as exc:
                logger.warning("Could not save release (%s)", type(exc).__name__)
                try:
                    latest = await self._latest_saved_release()
                except Exception as read_exc:
                    logger.warning(
                        "Could not check the release save (%s)",
                        type(read_exc).__name__,
                    )
                    self.release_source = "settings_store_unreadable"
                    raise ReleaseOutcomeUnknown from None
                if latest != [release]:
                    raise ReleaseNotSaved from None
                logger.info("The release was saved despite the error")
            self.release = release
            self.release_source = "store"
        logger.info(
            "Release changed: stable %s, canary %s at %s%%",
            release.stable,
            release.canary,
            release.canary_percent,
        )
        return release

    def usable(self, backend: Backend) -> bool:
        """Configured available and not disabled by the controller."""
        return backend.config.available and not self.controller.is_disabled(
            backend.config.name
        )

    def others_usable(
        self, backend: Backend, backends: Sequence[Backend] | None = None
    ) -> bool:
        pair = self.backends if backends is None else backends
        return any(self.usable(b) for b in pair if b is not backend)

    async def save_interventions(self) -> None:
        """Move the controller's new interventions to the store, in order."""
        if not self.controller.unsaved:
            return
        # One save at a time: a failed batch goes back in front of newer ones,
        # so the store keeps the controller's order.
        async with self.save_lock:
            items, self.controller.unsaved = self.controller.unsaved, []
            if not items:
                return
            try:
                await asyncio.wait_for(
                    self.store.save_interventions(items), self.store_timeout
                )
            except Exception as exc:
                # Keep them for the next attempt; the controller is unaffected.
                self.controller.unsaved = items + self.controller.unsaved
                logger.warning("Could not save interventions (%s)", type(exc).__name__)

    async def chat(self, message: str) -> ChatResponse:
        try:
            response, jev = await self._chat(message)
        finally:
            # Interventions also happen on requests that end in a 503.
            await self.save_interventions()
        try:
            # The Jev assessments are saved with their request or not at all, so
            # every saved assessment has a request to join to. A request that
            # ends in a 503 or is cancelled saves neither.
            await asyncio.wait_for(
                self.store.save_request(response, jev), self.store_timeout
            )
        except Exception as exc:
            # The customer still gets the reply; it just cannot be looked up later.
            logger.warning("Could not save request (%s)", type(exc).__name__)
        return response

    async def _call_jev(
        self, request_id: UUID, message: str, reasons: list[str]
    ) -> tuple[JevResult | None, list[JevAssessment]]:
        """Call Jev once and build both assessments. A None result means use
        the rule router; the reason is added to `reasons`."""
        call_id = uuid4()
        if not self.jev_breaker.allow():
            reasons.append("jev_circuit_open")
            self.metrics.jev_fallbacks.labels("jev_circuit_open").inc()
            return None, self._jev_records(
                request_id, call_id, status="skipped", skip_reason="circuit_open"
            )
        start = perf_counter()
        try:
            result = JevResult.model_validate(
                await asyncio.wait_for(self.router.route(message), self.jev_timeout)
            )
        except asyncio.CancelledError:
            # The request was cancelled, for example by a client disconnect.
            # Jev neither passed nor failed, and no record is written: the
            # monitor would count it as a Jev outage.
            self.jev_breaker.abandoned()
            raise
        except Exception as exc:
            # Only isolate the replaceable router call. Cancellation still propagates.
            # Do not log input text, credentials or provider exception messages.
            error_type = _jev_error_type(exc)
            logger.warning("Jev routing failed; using rule router (%s)", error_type)
            timed_out = isinstance(exc, TimeoutError)
            reason = "jev_timeout" if timed_out else "jev_error"
            reasons.append(reason)
            self.metrics.jev_fallbacks.labels(reason).inc()
            self.jev_breaker.failed()
            return None, self._jev_records(
                request_id,
                call_id,
                status="timeout" if timed_out else "error",
                error_type=error_type,
                start=start,
            )
        self.jev_breaker.succeeded()
        return result, self._jev_records(
            request_id, call_id, start=start, result=result
        )

    def _jev_records(
        self,
        request_id: UUID,
        call_id: UUID,
        *,
        # Used only when there is no result.
        status: Literal["timeout", "error", "skipped"] = "error",
        error_type: str | None = None,
        skip_reason: Literal["circuit_open"] | None = None,
        start: float | None = None,
        result: JevResult | None = None,
    ) -> list[JevAssessment]:
        """Build and check both assessments for one call.

        A record problem is logged, returns no records and never fails the
        customer's request.
        """
        call: dict[str, Any] = {
            "request_id": request_id,
            "call_id": call_id,
            "finished_at": datetime.now(UTC),
            "mocked": self.jev_mocked,
            "option_set_version": self.jev_option_set_version,
            "latency_ms": None if start is None else (perf_counter() - start) * 1000,
        }
        try:
            if result is None:
                records = [
                    JevAssessment(
                        **call,
                        question=question,
                        status=status,
                        error_type=error_type,
                        skip_reason=skip_reason,
                    )
                    for question in QUESTIONS
                ]
            else:
                call |= {
                    "mocked": result.mocked,
                    "jev_model": result.jev_model,
                    "provider_request_id": result.provider_request_id,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
                records = [
                    _assessment(call, "route", result.route),
                    _assessment(call, "intent", result.intent),
                ]
            check_call(records)
        except ValueError as exc:
            # ValidationError is a ValueError. Its text can hold Jev's answer.
            logger.warning("Could not build Jev records (%s)", type(exc).__name__)
            return []
        return records

    async def _chat(self, message: str) -> tuple[ChatResponse, list[JevAssessment]]:
        # Created before any provider call, so later records can share it.
        request_id = uuid4()
        start = perf_counter()
        version_id, canary = pick_version(self.release, request_id)
        backends = self.version_backends[version_id]
        reasons: list[str] = []
        router_name: Literal["mock_jev", "rule_router"] = "mock_jev"
        decision: RoutingDecision | None = None
        result, jev = await self._call_jev(request_id, message, reasons)
        if result is not None:
            # Each answer stands on its own: an invalid intent answer does not
            # stop Jev's route from being used.
            if result.route.error_type is None:
                decision = RoutingDecision.model_validate(
                    {
                        "route": result.route.choice,
                        "confidence": result.route.confidence,
                    }
                )
            else:
                logger.warning(
                    "Jev route answer was invalid; using rule router (%s)",
                    result.route.error_type,
                )
                reasons.append("jev_invalid_route")
                self.metrics.jev_fallbacks.labels("jev_invalid_route").inc()
        if decision is None:
            decision = await self.rule_router.route(message)
            router_name = "rule_router"
            jev_choice = None
        else:
            jev_choice = decision.route
        selected = next(b for b in backends if b.config.route == decision.route)
        action = None
        for backend in (selected, *(b for b in backends if b is not selected)):
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
                    backend.config.name,
                    can_disable=self.others_usable(backend, backends),
                )
                if backend is selected:
                    reasons.append("selected_backend_error")
        else:
            self.metrics.chat_unavailable.inc()
            self.metrics.chat_latency.observe(perf_counter() - start)
            raise BackendUnavailable
        intent, reply, facts = generated.intent, generated.reply, generated.policy_facts
        uncarded = (
            intent in self.labels.names
            and intent != UNKNOWN
            and intent not in self.cards
        )
        if uncarded:
            # The reply text is discarded, so it is not checked. Facts must be
            # empty: there is no card to ground them.
            format_errors = ["uncarded_policy_facts"] if facts else []
            reply = UNCARDED_REPLY
        else:
            format_errors = check_reply(reply)
            if intent not in self.labels.names:
                format_errors.append("unknown_label")
            elif intent == UNKNOWN and facts:
                # No intent means no card, so there is nothing to ground facts.
                format_errors.append("unknown_policy_facts")
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
                can_disable=self.others_usable(backend, backends),
            )
            or action
        )

        response = ChatResponse(
            id=request_id,
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
                    if intent == UNKNOWN
                    else "unsupported_topic"
                    if uncarded
                    else "policy_template"
                ),
                confidence=decision.confidence,
                latency_ms=round((perf_counter() - start) * 1000, 2),
                simulated_latency_ms=backend.config.latency_ms,
                format_check="failed" if format_errors else "passed",
                format_errors=format_errors,
                fallback_reason=", ".join(reasons) or None,
                controller_action=action,
                version_id=version_id,
                canary=canary,
            ),
        )
        trace = response.trace
        self.metrics.chat_requests.labels(
            trace.route, trace.model, trace.reply_source, trace.format_check
        ).inc()
        self.metrics.version_requests.labels(
            version_id, "canary" if canary else "stable", trace.format_check
        ).inc()
        self.metrics.chat_latency.observe(perf_counter() - start)
        return response, jev
