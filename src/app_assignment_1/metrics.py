"""Prometheus metrics for one gateway, served at the internal /metrics path.

Each gateway gets its own registry, so tests and restarts start from zero.
Labels are limited to fixed sets (route, model, reply source, format check,
reason, action). Never add message text, replies or request IDs as labels.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, get_args

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from app_assignment_1.schemas import BackendErrorCode, InterventionAction
from app_assignment_1.semantic import SAMPLES

if TYPE_CHECKING:
    from app_assignment_1.service import Gateway

ACTIONS = get_args(InterventionAction)
JEV_FALLBACK_REASONS = (
    "jev_timeout",
    "jev_error",
    "jev_invalid_route",
    "jev_circuit_open",
)


class GatewayMetrics:
    def __init__(self, gateway: "Gateway"):
        self.registry = CollectorRegistry()
        self.chat_requests = Counter(
            "gateway_chat_requests",
            "Chat requests answered, by route, model, reply source and format check.",
            ["route", "model", "reply_source", "format_check"],
            registry=self.registry,
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
        self.semantic_not_compared = Counter(
            "gateway_semantic_not_compared",
            "Replies that passed the format check but were not scored against "
            "Jev, because one of Jev and the model is a mock and the other real.",
            ["model"],
            registry=self.registry,
        )
        for backend in gateway.backends:
            self.semantic_not_compared.labels(backend.config.name)
        self.registry.register(_GatewayStateCollector(gateway))

    def render(self) -> bytes:
        return generate_latest(self.registry)


class _GatewayStateCollector(Collector):
    """Reads usage and controller state at scrape time, so values match /v1/health."""

    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway

    def collect(self) -> Iterable[Metric]:
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
