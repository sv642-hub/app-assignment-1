"""Gateway flow; requests, their Jev assessments and interventions go to a Store."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from app_assignment_1.backends import Backend, MockBackend
from app_assignment_1.breaker import CircuitBreaker
from app_assignment_1.config import Settings, settings_version
from app_assignment_1.intents import load_intents
from app_assignment_1.labels import UNKNOWN, load_labels
from app_assignment_1.metrics import GatewayMetrics
from app_assignment_1.monitor import Controller
from app_assignment_1.records import JevAssessment, check_call
from app_assignment_1.releases import Releases, load_catalog
from app_assignment_1.router import (
    JevAnswer,
    JevResult,
    MockJevRouter,
    Router,
    RuleRouter,
)
from app_assignment_1.schemas import (
    BackendConfig,
    BackendErrorCode,
    BackendName,
    BackendReply,
    ChatResponse,
    FaultRequest,
    JevFaultConfig,
    JevFaultRequest,
    JevUsage,
    ModelRelease,
    ModelUsage,
    PolicyCard,
    ReleaseEvent,
    RoutingDecision,
    Trace,
)
from app_assignment_1.semantic import SemanticMonitor, score
from app_assignment_1.store import MemoryStore, Store
from app_assignment_1.vllm import BackendCallError, BackendOutputError

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


class DeadlineExceeded(BackendUnavailable):
    """The request deadline cut short the alternate backend's attempt."""


class LastUsableBackend(Exception):
    """An operator asked to disable the only backend still answering."""


class NotAMock(Exception):
    """A fault was asked for on a backend that is not a mock."""


def backend_error_code(exc: Exception) -> BackendErrorCode:
    """Name a failed backend call. TimeoutError covers the vLLM client's
    BackendTimeoutError and the gateway's own per-attempt budget."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, BackendCallError | ConnectionError):
        return "call_failed"
    if isinstance(exc, BackendOutputError | ValidationError):
        return "invalid_output"
    return "other"


class ReleaseNotSaved(Exception):
    """An operator's release change could not be saved, so it was not made."""


class ReleaseSaveUnknown(Exception):
    """The save failed and could not be read back. The change was not made;
    the release the model kept waits to be saved as a newer change, so a
    restart after that keeps it too."""


# How often an operator change is planned again when the controller changes
# the same model during its save.
MAX_RELEASE_REPLANS = 5


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
    call: dict[str, Any],
    question: Literal["route", "intent"],
    answer: JevAnswer,
    option_order: list[str] | None = None,
) -> JevAssessment:
    if answer.error_type is not None:
        return JevAssessment(
            **call,
            question=question,
            status="error",
            error_type=answer.error_type,
            option_order=option_order,
        )
    return JevAssessment(
        **call,
        question=question,
        status="ok",
        choice=answer.choice,
        confidence=answer.confidence,
        probabilities=answer.probabilities,
        option_order=option_order,
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
        if settings.provider_mode == "real" and (
            backends is None or any(getattr(b, "mocked", True) for b in backends)
        ):
            # create_app builds the vLLM backends (real_backends.py).
            raise ValueError("APP_PROVIDER_MODE=real needs the real vLLM backends")
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
        self.request_deadline = settings.request_deadline_seconds
        self.alternate_reserve = settings.alternate_reserve_seconds
        self.jev_timeout = settings.jev_timeout_seconds
        self.jev_input_price = settings.jev_input_price_per_million_usd
        # Answered Jev calls and their tokens since start; see jev_usage().
        self.jev_answered_calls = 0
        self.jev_input_tokens = 0
        self.jev_output_tokens = 0
        self.store = store or MemoryStore(settings.request_store_limit)
        # Each store write waits at most this long, so a database outage
        # delays a reply by seconds, not by the pool's 30-second default.
        self.store_timeout = settings.store_timeout_seconds
        self.save_lock = asyncio.Lock()
        # Request saves still running after their request was cancelled. The
        # event loop keeps only weak references to tasks, so these keep them.
        self.pending_saves: set[asyncio.Task[None]] = set()
        intents = load_intents(settings.intents_path)
        self.cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
            settings.policy_cards_path.read_text()
        )
        if set(self.cards) != {item.intent for item in intents}:
            raise ValueError("Policy cards must match the configured intents exactly")
        self.labels = load_labels(settings.labels_path)
        if not set(self.cards) <= self.labels.names:
            raise ValueError("Every intent with a policy card must be a label")
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
        self.semantic = SemanticMonitor(
            (b.config.name for b in self.backends), settings
        )
        self.settings = settings
        self.settings_version = self._settings_version()
        if settings.semantic_actions_enabled:
            # A re-enabled model starts both windows again, or one old run of
            # disagreements would disable it on its next reply. With actions
            # off, the semantic report keeps its window across a cooldown.
            self.controller.on_reenable.append(self.semantic.reset)
        self.releases = Releases(
            self.backends,
            load_catalog(settings.releases_path),
            self.controller.watch_samples,
        )
        self.controller.roll_back = self.releases.roll_back
        self.controller.on_watched.append(self.releases.watched)
        self.controller.on_confirm.append(self.releases.confirm)
        self.releases.on_change.append(self._release_changed)
        # One operator release change at a time. The chat path never takes it.
        self.release_lock = asyncio.Lock()
        self.metrics = GatewayMetrics(self)

    def _release_changed(self, event: ReleaseEvent) -> None:
        """A model now serves another version: its monitor windows describe
        the old one, so both start again. A deploy starts the watch."""
        if event.action == "confirmed":
            return
        if event.action == "deployed":
            self.controller.watch(event.backend)
        else:
            self.controller.unwatch(event.backend)
        self.semantic.reset(event.backend)

    def jev_usage(self) -> JevUsage:
        cost = (
            None
            if self.jev_input_price is None
            else self.jev_input_tokens * self.jev_input_price / 1_000_000
        )
        return JevUsage(
            mocked=self.jev_mocked,
            answered_calls=self.jev_answered_calls,
            input_tokens=self.jev_input_tokens,
            output_tokens=self.jev_output_tokens,
            estimated_cost_usd=cost,
        )

    def usable(self, backend: Backend) -> bool:
        """Configured available and not disabled by the controller."""
        return backend.config.available and not self.controller.is_disabled(
            backend.config.name
        )

    def _semantic_reason(self, name: BackendName) -> str | None:
        """Why the controller should act on this model, or None if it is not
        above the baseline."""
        status = self.semantic.status(name)
        baseline = self.semantic.rules.baseline_disagreement_rate
        if status.status != "above_baseline" or baseline is None:
            return None
        scored = status.agreements + status.disagreements
        return (
            f"intent differs from Jev's on {status.disagreement_rate:.0%} of last "
            f"{scored} scored replies (baseline {baseline:.0%})"
        )

    def others_usable(self, backend: Backend) -> bool:
        return any(self.usable(b) for b in self.backends if b is not backend)

    def others_answering(self, backend: Backend) -> bool:
        """For a semantic disable: another model is usable and has answered
        recently without failing. "Usable" alone is not enough: a model that
        fails every call stays usable until its failure window fills, and
        disabling the only model that answers would turn wrong-but-valid
        replies into 503s."""
        return any(
            self.usable(b) and self.controller.answering(b.config.name)
            for b in self.backends
            if b is not backend
        )

    def backend(self, name: BackendName) -> Backend:
        return next(b for b in self.backends if b.config.name == name)

    def operator_disable(self, name: BackendName, reason: str) -> None:
        """Hold a model disabled until operator_enable. Like the controller, it
        never disables the last usable model."""
        if not self.others_usable(self.backend(name)):
            raise LastUsableBackend(name)
        self.controller.operator_disable(name, reason)

    def operator_enable(self, name: BackendName, reason: str) -> None:
        self.controller.operator_enable(name, reason)

    def _settings_version(self) -> str:
        """The models' live config stands in for backends.json, so a fault
        set through /v1/faults gives later requests a new ID."""
        s = self.settings
        return settings_version(
            s,
            (s.intents_path, s.labels_path, s.policy_cards_path),
            [b.config.model_dump(mode="json") for b in self.backends],
        )

    def set_fault(self, request: FaultRequest) -> BackendConfig:
        """Change a mock model's simulated fault rates while it runs."""
        backend = self.backend(request.model)
        if not isinstance(backend, MockBackend):
            raise NotAMock(request.model)
        config = backend.config
        if request.fault == "none":
            config.error_rate = config.fault_rate = config.label_swap_rate = 0
        elif request.fault == "outage":
            config.error_rate = request.rate
        elif request.fault == "bad_format":
            config.fault_rate = request.rate
        else:
            config.label_swap_rate = request.rate
        self.settings_version = self._settings_version()
        logger.info(
            "Operator set fault %s=%s on %s", request.fault, request.rate, request.model
        )
        return config

    async def deploy_release(
        self, name: BackendName, version: str, reason: str
    ) -> ModelRelease:
        """Deploy a version to a model and start watching it. Raises
        UnknownVersion, AlreadyServing, ReleaseNotSaved or ReleaseSaveUnknown."""
        await self._operator_change(
            name, lambda: self.releases.plan_deploy(name, version, reason)
        )
        return self.releases.state[name].model_copy()

    async def rollback_release(self, name: BackendName, reason: str) -> ModelRelease:
        """Put a model back on its rollback target. Raises NoRollbackTarget,
        ReleaseNotSaved or ReleaseSaveUnknown."""
        await self._operator_change(
            name, lambda: self.releases.plan_rollback(name, reason, "operator")
        )
        return self.releases.state[name].model_copy()

    async def _operator_change(
        self, name: BackendName, plan: Callable[[], ReleaseEvent]
    ) -> None:
        """One operator change at a time (release_lock). Only operators take
        that lock: chats never wait for an operator's store write."""
        async with self.release_lock:
            # A cancel, such as a client disconnect, waits for the whole
            # change, so a saved change is never left unapplied.
            await self._finish(self._plan_save_apply(name, plan), hold=True)

    async def _plan_save_apply(
        self, name: BackendName, plan: Callable[[], ReleaseEvent]
    ) -> None:
        """Plan, save, then apply, so the operator's answer, the running
        gateway and a restart agree.

        Every change carries a revision, taken when it is planned, and a
        restart loads the highest saved one per model. The save is the only
        await. If the controller rolls back or confirms the model meanwhile,
        its event gets the higher revision, so the stale operator event can
        never win a restart; the change is planned again on the new state, with
        a newer revision, and saved again. Each controller change ends a watch,
        so this repeats at most a few times."""
        for _ in range(MAX_RELEASE_REPLANS):
            event = plan()
            before = self.releases.state[name]
            await self._save_release_change(event)
            if self.releases.state[name] is before:
                self.releases.apply(event)
                logger.info(
                    "Operator %s %s: now %s",
                    event.action.replace("_", " "),
                    name,
                    event.version,
                )
                return
        logger.warning("Release change for %s kept racing the controller", name)
        raise ReleaseNotSaved

    async def _save_release_change(self, event: ReleaseEvent) -> None:
        """Save one operator change. It does not take save_lock, so chats and
        their saves never wait for it; revisions, not write order, decide what
        a restart loads.

        A write that fails or times out may have been saved anyway. It is read
        back: if the store has it, the change goes ahead. Otherwise the change
        is not made, and the release the model kept is queued as a newer
        change, so even a write that lands late can't win a restart. Raises
        ReleaseNotSaved, or ReleaseSaveUnknown if the read-back fails too."""
        try:
            await asyncio.wait_for(
                self.store.save_release_events([event]), self.store_timeout
            )
            return
        except Exception as exc:
            logger.warning("Could not save release change (%s)", type(exc).__name__)
        try:
            latest = await asyncio.wait_for(
                self.store.latest_release_events(), self.store_timeout
            )
        except Exception as exc:
            logger.warning(
                "Could not read the release change back (%s)", type(exc).__name__
            )
            self._keep_release(event)
            raise ReleaseSaveUnknown from None
        if event in latest:
            logger.info("Release change was saved despite the error")
            return
        self._keep_release(event)
        raise ReleaseNotSaved

    def _keep_release(self, event: ReleaseEvent) -> None:
        current = self.releases.state[event.backend].version
        self.releases.kept(
            event.backend,
            f"change to {event.version} not confirmed as saved; kept {current}",
        )

    async def restore_releases(self) -> None:
        """At startup: serve the release each model had before the restart. A
        store problem is logged and every model starts on base."""
        try:
            latest = await asyncio.wait_for(
                self.store.latest_release_events(), self.store_timeout
            )
        except Exception as exc:
            logger.warning("Could not load releases (%s)", type(exc).__name__)
            return
        self.releases.restore(latest)

    def set_jev_fault(self, request: JevFaultRequest) -> JevFaultConfig:
        """Change the mock Jev router's simulated faults while it runs."""
        router = self.router
        if not isinstance(router, MockJevRouter):
            raise NotAMock("jev")
        if request.fault == "none":
            faults = JevFaultConfig()
        elif request.fault == "outage":
            faults = router.faults.model_copy(update={"error_rate": request.rate})
        else:
            faults = router.faults.model_copy(
                update={"slow_rate": request.rate, "slow_seconds": request.seconds}
            )
        # Replaced, not changed in place, so a call in flight keeps its faults.
        router.faults = faults
        logger.info("Operator set Jev fault %s=%s", request.fault, request.rate)
        return faults

    async def save_interventions(self) -> None:
        """Move the controller's new interventions, then the release changes
        it made, to the store, in order."""
        await self._save_interventions()
        if not self.releases.unsaved:
            return
        async with self.save_lock:
            events, self.releases.unsaved = self.releases.unsaved, []
            if not events:
                return
            try:
                await asyncio.wait_for(
                    self.store.save_release_events(events), self.store_timeout
                )
            except Exception as exc:
                self.releases.unsaved = events + self.releases.unsaved
                logger.warning(
                    "Could not save release changes (%s)", type(exc).__name__
                )

    async def _save_interventions(self) -> None:
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
        except BaseException:
            # Interventions also happen on requests that end in a 503.
            await self._finish(self.save_interventions())
            raise
        # By now the failure monitor and the metrics have counted this reply,
        # so a cancel must not stop the saves: the saved requests would no
        # longer match the counts, and the controller has already handed its
        # interventions over. Both saves run as one task, in order.
        await self._finish(self._save_all(response, jev))
        return response

    async def _save_all(
        self, response: ChatResponse, jev: Sequence[JevAssessment]
    ) -> None:
        await self.save_interventions()
        await self._save_request(response, jev)

    async def _finish(
        self, saving: Coroutine[Any, Any, None], hold: bool = False
    ) -> None:
        """Run a save as its own task. A cancel waits for it, then passes on.

        store_timeout bounds each store call, not the whole wait: a save can
        first queue on save_lock behind other intervention saves, so during a
        store outage the k-th queued request waits about k * store_timeout."""
        save = asyncio.create_task(saving)
        self.pending_saves.add(save)
        save.add_done_callback(self.pending_saves.discard)
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError:
            # Normally a second cancel stops the wait; the save keeps running
            # in pending_saves. With `hold`, wait through every cancel: the
            # caller holds a lock that must not be released mid-save.
            while not save.done():
                try:
                    await asyncio.wait({save})
                except asyncio.CancelledError:
                    if not hold:
                        raise
            raise

    async def finish_saves(self) -> None:
        """At shutdown, before the store closes: wait for saves still running,
        such as those of requests the server cancelled, then save anything
        noticed since the last request (a /metrics scrape can see a re-enable)."""
        while self.pending_saves:
            await asyncio.wait(set(self.pending_saves))
        await self.save_interventions()

    async def _save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment]
    ) -> None:
        try:
            # The Jev assessments are saved with their request or not at all, so
            # every saved assessment has a request to join to. A request that
            # ends in a 503 or is cancelled before its reply saves neither.
            await asyncio.wait_for(
                self.store.save_request(response, jev), self.store_timeout
            )
        except Exception as exc:
            # The customer still gets the reply; it just cannot be looked up later.
            logger.warning("Could not save request (%s)", type(exc).__name__)

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
        self.jev_answered_calls += 1
        self.jev_input_tokens += result.input_tokens
        self.jev_output_tokens += result.output_tokens
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
                    _assessment(
                        call, "route", result.route, option_order=result.route_order
                    ),
                    _assessment(call, "intent", result.intent),
                ]
            check_call(records)
        except ValueError as exc:
            # ValidationError is a ValueError. Its text can hold Jev's answer.
            logger.warning("Could not build Jev records (%s)", type(exc).__name__)
            return []
        return records

    def _fallback_left(self, order: Sequence[Backend], current: Backend) -> bool:
        """Another model could still answer after `current`: usable now, or a
        last resort."""
        return any(
            b is not current
            and b.config.available
            and (self.usable(b) or self.controller.last_resort(b.config.name))
            for b in order
        )

    async def _attempt(
        self,
        backend: Backend,
        message: str,
        budget: float,
        attempt: int,
        action: str | None,
        own_route: bool,
    ) -> tuple[BackendReply | None, str | None, bool, int]:
        """Call one backend within `budget` seconds. Return its reply (None if
        the call failed), the latest controller action, whether the deadline
        cut short a later attempt, and the release generation the call
        started on."""
        name = backend.config.name
        generation = self.releases.generation[name]
        usage = self.usage[name]
        usage.attempted_calls += 1
        timer = asyncio.timeout(budget)
        try:
            async with timer:
                generated = BackendReply.model_validate(await backend.generate(message))
        except Exception as exc:
            if timer.expired() and attempt > 1:
                # A later attempt had a shortened opportunity. Do not score this
                # as a model failure or a successful monitor sample.
                usage.deadline_exceeded_calls += 1
                return None, action, True, generation
            usage.failed_calls += 1
            code = backend_error_code(exc)
            usage.failed_by_error[code] = usage.failed_by_error.get(code, 0) + 1
            # A reply that fails BackendReply validation also counts as a call
            # error. Cancellation still propagates. Do not log input text or
            # provider exception messages.
            logger.warning(
                "Backend %s failed: %s (%s)",
                backend.config.name,
                code,
                type(exc).__name__,
            )
            # No await from here to the return: the check and the record see
            # one release state, without holding a lock on the reply path.
            if self.releases.generation[name] != generation:
                # The call started on a version the model no longer serves;
                # its failure says nothing about the new one.
                return None, action, False, generation
            action = (
                self.controller.record_error(
                    name,
                    can_disable=self.others_usable(backend),
                    own_route=own_route,
                )
                or action
            )
            self._finish_release_watch(backend)
            return None, action, False, generation
        usage.completed_calls += 1
        return generated, action, False, generation

    def _finish_release_watch(self, backend: Backend) -> None:
        name = backend.config.name
        count = self.controller.watching.get(name)
        if count is None or count < self.controller.watch_samples:
            return
        rules = self.semantic.rules
        if (
            rules.actions_enabled
            and rules.baseline_disagreement_rate is not None
            and self.jev_mocked == getattr(backend, "mocked", True)
            and self.semantic.status(name).status != "normal"
        ):
            # A well-formed reply can carry no usable semantic evidence.
            return
        self.controller.finish_call(name)

    async def _chat(self, message: str) -> tuple[ChatResponse, list[JevAssessment]]:
        # Created before any provider call, so later records can share it.
        request_id = uuid4()
        start = perf_counter()
        reasons: list[str] = []
        router_name: Literal["mock_jev", "jev", "rule_router"] = (
            "mock_jev" if self.jev_mocked else "jev"
        )
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
        selected = next(b for b in self.backends if b.config.route == decision.route)
        order = (selected, *(b for b in self.backends if b is not selected))
        action = None
        generated: BackendReply | None = None
        attempted_latency = 0
        tried: list[Backend] = []
        deadline_exceeded = False
        reserve_held = False
        # Two passes. First, the models that are usable now. If none answered,
        # every model not called yet:
        # - one whose cooldown ended while another model's call ran is usable
        #   again;
        # - one disabled only for wrong intents still gives a well-formed
        #   reply, which beats a 503. The other model can start failing after
        #   the semantic disable, and the controller cannot see that coming.
        for last_pass in (False, True):
            for backend in order:
                if backend in tried:
                    continue
                if not last_pass:
                    if not self.usable(backend):
                        if backend is selected:
                            reasons.append(
                                "selected_backend_disabled"
                                if backend.config.available
                                else "selected_backend_unavailable"
                            )
                        continue
                else:
                    if not backend.config.available:
                        continue
                    if not self.usable(backend):
                        if not self.controller.last_resort(backend.config.name):
                            continue
                        reasons.append("semantic_last_resort")
                remaining = self.request_deadline - (perf_counter() - start)
                if remaining <= 0:
                    # The deadline cut this model out only if time was held
                    # back for it. If the last attempt had all the time,
                    # because no model could follow it then, its timeout was a
                    # failure and this is the usual 503.
                    deadline_exceeded = not tried or reserve_held
                    break
                # Hold back the reserve only on the first attempt, and only when
                # another model, a last resort included, could still answer.
                # Otherwise this is the last chance, so it gets all the time left.
                budget = (
                    remaining - self.alternate_reserve
                    if not tried and self._fallback_left(order, backend)
                    else remaining
                )
                if budget <= 0:
                    deadline_exceeded = True
                    break
                reserve_held = budget < remaining
                tried.append(backend)
                attempted_latency = backend.config.latency_ms
                generated, action, cut_short, generation = await self._attempt(
                    backend, message, budget, len(tried), action, backend is selected
                )
                if generated is not None:
                    break
                if cut_short:
                    deadline_exceeded = True
                    break
                if backend is selected and not last_pass:
                    reasons.append("selected_backend_error")
            if generated is not None or deadline_exceeded:
                break
        if deadline_exceeded:
            self.metrics.chat_unavailable.inc()
            self.metrics.chat_latency.observe(perf_counter() - start)
            raise DeadlineExceeded
        if generated is None:
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
        # No await in this block: the generation check, the monitors and the
        # watch all see one release state, and the reply path holds no lock,
        # so an operator's store write never delays a customer.
        # A reply from a call that started on a version the model no longer
        # serves goes to the customer, but the monitors skip it: it says
        # nothing about the new version.
        current = self.releases.generation[backend.config.name] == generation
        # The trace reports validation of the original generated reply.
        if current:
            action = (
                self.controller.record(
                    backend.config.name,
                    bool(format_errors),
                    can_disable=self.others_usable(backend),
                    own_route=backend is selected,
                )
                or action
            )
        if current and not format_errors:
            # Score the model's own label against Jev's intent answer. A failed
            # format check gives no semantic sample; the controller counted it.
            # Only like with like: a mock model against mock Jev, or a real
            # model against real Jev. A mix disagrees by design, so it is never
            # scored and never acted on.
            if self.jev_mocked == getattr(backend, "mocked", True):
                sample = score(generated.intent, jev)
                rerouted = backend is not selected
                self.semantic.record(backend.config.name, sample, rerouted)
                self.metrics.semantic_samples.labels(
                    backend.config.name, sample, "rerouted" if rerouted else "own"
                ).inc()
                # Only replies on the model's own route can trigger an action. A
                # rerouted reply can follow a one-off error from the selected
                # model, which is still usable, so it must not disable the model
                # that served it.
                if self.semantic.rules.actions_enabled and not rerouted:
                    action = (
                        self.controller.record_semantic(
                            backend.config.name,
                            self._semantic_reason(backend.config.name),
                            can_disable=self.others_answering(backend),
                        )
                        or action
                    )
            else:
                self.semantic.skip(backend.config.name)
                self.metrics.semantic_not_compared.labels(backend.config.name).inc()
        if current:
            # After the semantic check, so it can roll back on the watch's
            # last call before the release is confirmed.
            self._finish_release_watch(backend)

        mocked = getattr(backend, "mocked", True)
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
                    if mocked
                    else "generated"
                ),
                confidence=decision.confidence,
                latency_ms=round((perf_counter() - start) * 1000, 2),
                simulated_latency_ms=attempted_latency if mocked else 0,
                format_check="failed" if format_errors else "passed",
                format_errors=format_errors,
                fallback_reason=", ".join(reasons) or None,
                controller_action=action,
                mocked=mocked,
                settings_version=self.settings_version,
            ),
        )
        trace = response.trace
        self.metrics.chat_requests.labels(
            trace.route, trace.model, trace.reply_source, trace.format_check
        ).inc()
        self.metrics.chat_latency.observe(perf_counter() - start)
        return response, jev
