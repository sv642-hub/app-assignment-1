"""Prometheus metrics for one gateway, served at the internal /metrics path.

Each gateway gets its own registry, so tests and restarts start from zero.
Labels are limited to fixed sets (route, model, reply source, format check,
reason, action). Never add message text, replies or request IDs as labels.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, get_args

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from app_assignment_1.schemas import BackendErrorCode, InterventionAction, Route, Trace
from app_assignment_1.semantic import SAMPLES

if TYPE_CHECKING:
    from app_assignment_1.service import Gateway

# The problems service.check_reply can return.
TEXT_PROBLEMS = ("empty_reply", "reply_too_long", "unfilled_placeholder")

ACTIONS = get_args(InterventionAction)
JEV_FALLBACK_REASONS = (
    "jev_timeout",
    "jev_error",
    "jev_invalid_route",
    "jev_circuit_open",
)
# Includes 2 s and 4 s, the latency targets in docs/MONITORING.md, so the
# dashboard can count requests under each target exactly.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)
# The label values of gateway_chat_requests, read from the Trace fields.
REPLY_SOURCES = get_args(Trace.model_fields["reply_source"].annotation)
FORMAT_CHECKS = get_args(Trace.model_fields["format_check"].annotation)
JEV_OUTCOMES = ("ok", "timeout", "error")
CALL_OUTCOMES = ("ok", "failed")
DETECT_REASONS = ("timeout", "call_error", "format_failure", "failure")
# Seconds from a model's first failure to its disable; 30 s is the draft target.
DETECTION_BUCKETS = (1, 2, 4, 8, 12, 16, 20, 30, 45, 60, 120, 240)
REPLY_CHECK_SKIP_REASONS = (
    "no_first_answer",
    "format_fallback",
    "breaker_not_closed",
    "busy",
)
AGREEMENT_RESULTS = ("agree", "disagree", "no_signal")
FIT_RESULTS = ("fits", "does_not_fit", "no_signal")
AUTH_FAILURE_REASONS = ("operator_disabled", "missing_or_wrong_token")


# What happened when the small model's score was below the threshold.
# escalated: the large model's reply was used. shadow: escalation is off, so
# the small reply was kept. large_unusable, no_time, large_failed and
# large_format_failed: the large model could not be used, so the small reply
# was kept.
ESCALATION_OUTCOMES = (
    "escalated",
    "shadow",
    "large_unusable",
    "no_time",
    "large_failed",
    "large_format_failed",
)


class GatewayMetrics:
    """The Prometheus metrics for one gateway, kept in its own registry."""

    def __init__(self, gateway: "Gateway"):
        """Register the gateway's counters, histograms and state collector."""
        self.registry = CollectorRegistry()
        self.chat_requests = Counter(
            "gateway_chat_requests",
            "Chat requests answered, by route, model, reply source and format check.",
            ["route", "model", "reply_source", "format_check"],
            registry=self.registry,
        )
        # Create every label set at 0, so rate() and increase() count a burst
        # that lands before the first scrape. Combinations that cannot happen
        # stay at 0.
        for route in get_args(Route):
            for backend in gateway.backends:
                for source in REPLY_SOURCES:
                    for check in FORMAT_CHECKS:
                        self.chat_requests.labels(
                            route, backend.config.name, source, check
                        )
        self.chat_unavailable = Counter(
            "gateway_chat_unavailable",
            "Chat requests that got a 503 because no backend could answer.",
            registry=self.registry,
        )
        self.chat_latency = Histogram(
            "gateway_chat_latency_seconds",
            "Time to handle a chat request inside the gateway, including 503s.",
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=self.registry,
        )
        self.chat_latency_by_model = Histogram(
            "gateway_chat_answer_latency_seconds",
            "Time to handle a chat request that was answered, by route and model. "
            "Unlike gateway_chat_latency_seconds it leaves out 503s.",
            ["route", "model"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.backend_call_latency = Histogram(
            "gateway_backend_call_latency_seconds",
            "Time of one call to a Qwen model, by model and outcome (ok or "
            "failed). A call cut short by the request deadline is not counted.",
            ["model", "outcome"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.fault_detection = Histogram(
            "gateway_fault_detection_seconds",
            "Seconds from a model's first failure to the controller disabling "
            "it, by model and reason (timeout, call_error, format_failure or "
            "failure for a mix). Semantic disables have no onset and are not "
            "counted.",
            ["model", "reason"],
            buckets=DETECTION_BUCKETS,
            registry=self.registry,
        )
        for backend in gateway.backends:
            for reason in DETECT_REASONS:
                self.fault_detection.labels(backend.config.name, reason)
        self.jev_call_latency = Histogram(
            "gateway_jev_call_latency_seconds",
            "Time of one Jev call, by outcome: ok, timeout or error. Skipped "
            "calls (circuit open) are not counted.",
            ["outcome"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        for outcome in JEV_OUTCOMES:
            self.jev_call_latency.labels(outcome)
        self.chat_in_flight = Gauge(
            "gateway_chat_in_flight",
            "Chat requests being handled right now. A hung call shows here "
            "before it shows in any latency histogram.",
            registry=self.registry,
        )
        self.operator_auth_failures = Counter(
            "gateway_operator_auth_failures",
            "Operator route calls refused: operator_disabled (no token is "
            "configured, 403) or missing_or_wrong_token (401).",
            ["reason"],
            registry=self.registry,
        )
        for reason in AUTH_FAILURE_REASONS:
            self.operator_auth_failures.labels(reason)
        for route in get_args(Route):
            for backend in gateway.backends:
                self.chat_latency_by_model.labels(route, backend.config.name)
        for backend in gateway.backends:
            for outcome in CALL_OUTCOMES:
                self.backend_call_latency.labels(backend.config.name, outcome)
        self.reply_check_latency = Histogram(
            "gateway_reply_check_latency_seconds",
            "Time of one second Jev call (the reply check), by outcome: ok, "
            "timeout or error. It runs after the reply, so customers never wait "
            "for it.",
            ["outcome"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        for outcome in JEV_OUTCOMES:
            self.reply_check_latency.labels(outcome)
        self.reply_check_skipped = Counter(
            "gateway_reply_check_skipped",
            "Requests the second Jev call was not made for, by reason.",
            ["reason"],
            registry=self.registry,
        )
        for reason in REPLY_CHECK_SKIP_REASONS:
            self.reply_check_skipped.labels(reason)
        self.reply_check_agreement = Counter(
            "gateway_reply_check_agreement",
            "First Jev call (before the model) against the second (after it), "
            "by question: agree, disagree, or no_signal when either call gave "
            "no valid answer.",
            ["question", "result"],
            registry=self.registry,
        )
        for question in ("route", "intent"):
            for result in AGREEMENT_RESULTS:
                self.reply_check_agreement.labels(question, result)
        self.reply_check_fit = Counter(
            "gateway_reply_check_fit",
            "Whether the second Jev call judged the reply to fit the customer's "
            "message: fits, does_not_fit, or no_signal.",
            ["result"],
            registry=self.registry,
        )
        for result in FIT_RESULTS:
            self.reply_check_fit.labels(result)
        self.jev_fallbacks = Counter(
            "gateway_jev_fallbacks",
            "Requests routed by the rule router: the Jev call failed, its route "
            "answer was invalid, or the circuit breaker skipped Jev.",
            ["reason"],
            registry=self.registry,
        )
        for reason in JEV_FALLBACK_REASONS:
            self.jev_fallbacks.labels(reason)
        self.semantic_samples = Counter(
            "gateway_semantic_samples",
            "Replies that passed the format check, scored against Jev's intent "
            "answer: agree, disagree or no_signal. traffic is own for the model's "
            "route, rerouted for the other route's messages.",
            ["model", "sample", "traffic"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            for sample in SAMPLES:
                for traffic in ("own", "rerouted"):
                    self.semantic_samples.labels(backend.config.name, sample, traffic)
        self.uncarded_facts_dropped = Counter(
            "gateway_uncarded_facts_dropped",
            "Replies for an intent with no policy card that listed policy facts "
            "anyway. The facts are dropped and the fixed uncarded reply is shown, "
            "so these pass the format check.",
            ["model"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            self.uncarded_facts_dropped.labels(backend.config.name)
        self.empty_unknown_filled = Counter(
            "gateway_empty_unknown_filled",
            "Replies with intent unknown, no text and no policy facts. The gateway "
            "shows the fixed no-match reply, so these pass the format check.",
            ["model"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            self.empty_unknown_filled.labels(backend.config.name)
        self.discarded_text_problems = Counter(
            "gateway_discarded_text_problems",
            "Problems in the model's own reply text, which the gateway discards: "
            "the reply is built from the policy card. These do not fail the "
            "format check. One reply can count under more than one problem.",
            ["model", "problem"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            for problem in TEXT_PROBLEMS:
                self.discarded_text_problems.labels(backend.config.name, problem)
        self.semantic_not_compared = Counter(
            "gateway_semantic_not_compared",
            "Replies that passed the format check but were not scored against "
            "Jev, because one of Jev and the model is a mock and the other real.",
            ["model"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            self.semantic_not_compared.labels(backend.config.name)
        self.escalation_score = Histogram(
            "gateway_escalation_score",
            "The small model's intent confidence on replies that passed the "
            "format check, when it gave one. Recorded with escalation on or off.",
            buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1),
            registry=self.registry,
        )
        self.escalations = Counter(
            "gateway_escalations",
            "Small-model replies whose score was below the escalation "
            "threshold, by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        for outcome in ESCALATION_OUTCOMES:
            self.escalations.labels(outcome)
        self.registry.register(_GatewayStateCollector(gateway))

    def render(self) -> bytes:
        """Return the metrics in the Prometheus text format."""
        return generate_latest(self.registry)


class _GatewayStateCollector(Collector):
    """Reads usage and controller state at scrape time, so values match /v1/health."""

    def __init__(self, gateway: "Gateway"):
        """Keep the gateway whose state is read at each scrape."""
        self.gateway = gateway

    def collect(self) -> Iterable[Metric]:
        """Yield gauges for the gateway's current state."""
        gateway = self.gateway
        calls = {
            field: CounterMetricFamily(
                f"gateway_backend_calls_{field}",
                f"Backend calls {field}, per model.",
                labels=["model"],
            )
            for field in ("attempted", "completed", "failed")
        }
        for usage in gateway.usage.values():
            calls["attempted"].add_metric([usage.name], usage.attempted_calls)
            calls["completed"].add_metric([usage.name], usage.completed_calls)
            calls["failed"].add_metric([usage.name], usage.failed_calls)
        yield from calls.values()

        # Every code for every model, so a rate query sees zeros, not gaps.
        errors = CounterMetricFamily(
            "gateway_backend_errors",
            "Failed backend calls, by model and error code.",
            labels=["model", "error"],
        )
        for usage in gateway.usage.values():
            for code in get_args(BackendErrorCode):
                errors.add_metric(
                    [usage.name, code], usage.failed_by_error.get(code, 0)
                )
        yield errors

        disabled = GaugeMetricFamily(
            "gateway_model_disabled",
            "1 if the controller has disabled the model, else 0.",
            labels=["model"],
        )
        rate = GaugeMetricFamily(
            "gateway_monitor_failure_rate",
            "Share of calls in the monitor window that failed (0 when empty).",
            labels=["model"],
        )
        samples = GaugeMetricFamily(
            "gateway_monitor_window_samples",
            "Calls currently in the monitor window.",
            labels=["model"],
        )
        for stat in gateway.controller.stats():
            disabled.add_metric([stat.name], int(stat.disabled_by_controller))
            rate.add_metric([stat.name], stat.failure_rate or 0)
            samples.add_metric([stat.name], stat.samples)
        yield from (disabled, rate, samples)

        interventions = CounterMetricFamily(
            "gateway_controller_interventions",
            "Controller interventions, by action and model.",
            labels=["action", "model"],
        )
        counts = gateway.controller.action_counts
        for name in gateway.controller.windows:
            for action in ACTIONS:
                interventions.add_metric([action, name], counts[(action, name)])
        yield interventions

        # One series per model, for the version it serves now. Versions come
        # from the release catalog, a fixed set.
        release = GaugeMetricFamily(
            "gateway_model_release",
            "1 for the version each model serves, with the release status: "
            "stable, watching or rolled_back.",
            labels=["model", "version", "status"],
        )
        for item in gateway.releases.stats():
            release.add_metric([item.name, item.version, item.status], 1)
        yield release

        # One label value per process: mocked Jev reports 0 tokens, and the
        # label keeps mock runs apart when series are compared later.
        jev = gateway.jev_usage()
        mocked = "true" if jev.mocked else "false"
        answered = CounterMetricFamily(
            "gateway_jev_answered_calls",
            "Jev calls that returned an answer; each call counts once.",
            labels=["mocked"],
        )
        answered.add_metric([mocked], jev.answered_calls)
        tokens = CounterMetricFamily(
            "gateway_jev_tokens",
            "Tokens of answered Jev calls, by direction.",
            labels=["direction", "mocked"],
        )
        tokens.add_metric(["input", mocked], jev.input_tokens)
        tokens.add_metric(["output", mocked], jev.output_tokens)
        yield from (answered, tokens)
        # Absent while APP_JEV_INPUT_PRICE_PER_MILLION_USD is unset, so an
        # unknown price never reads as $0.
        if jev.estimated_cost_usd is not None:
            cost = CounterMetricFamily(
                "gateway_jev_estimated_cost_usd",
                "Estimated Jev cost: input tokens times the configured price.",
                labels=["mocked"],
            )
            cost.add_metric([mocked], jev.estimated_cost_usd)
            yield cost

        # The second Jev call. Its tokens and cost are kept apart from the first
        # call's, so the price of turning the check on is visible.
        check = gateway.reply_check_usage()
        enabled = GaugeMetricFamily(
            "gateway_reply_check_enabled",
            "1 when the second Jev call (reply check) is switched on.",
        )
        enabled.add_metric([], int(check.enabled))
        check_calls = CounterMetricFamily(
            "gateway_reply_check_answered_calls",
            "Second Jev calls that returned an answer.",
            labels=["mocked"],
        )
        check_calls.add_metric([mocked], check.answered_calls)
        check_tokens = CounterMetricFamily(
            "gateway_reply_check_tokens",
            "Tokens of answered second Jev calls, by direction.",
            labels=["direction", "mocked"],
        )
        check_tokens.add_metric(["input", mocked], check.input_tokens)
        check_tokens.add_metric(["output", mocked], check.output_tokens)
        yield from (enabled, check_calls, check_tokens)
        if check.estimated_cost_usd is not None:
            check_cost = CounterMetricFamily(
                "gateway_reply_check_estimated_cost_usd",
                "Estimated cost of second Jev calls: input tokens times the "
                "configured price.",
                labels=["mocked"],
            )
            check_cost.add_metric([mocked], check.estimated_cost_usd)
            yield check_cost

        # Only models with data get a value: an empty window is not 0%.
        disagreement = GaugeMetricFamily(
            "gateway_semantic_disagreement_rate",
            "Share of scored replies in the semantic window whose intent differs "
            "from Jev's. Absent until a reply is scored.",
            labels=["model"],
        )
        coverage = GaugeMetricFamily(
            "gateway_semantic_coverage",
            "Share of the semantic window that had a usable Jev intent answer. "
            "Absent while the window is empty.",
            labels=["model"],
        )
        above = GaugeMetricFamily(
            "gateway_semantic_above_baseline",
            "1 if the model's disagreement rate is above baseline plus margin.",
            labels=["model"],
        )
        for model in gateway.semantic.stats():
            if model.disagreement_rate is not None:
                disagreement.add_metric([model.name], model.disagreement_rate)
            if model.coverage is not None:
                coverage.add_metric([model.name], model.coverage)
            above.add_metric([model.name], int(model.status == "above_baseline"))
        yield from (disagreement, coverage, above)
