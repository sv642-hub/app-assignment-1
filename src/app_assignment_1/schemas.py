"""Validated API contracts and on-disk mock configuration."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Route = Literal["standard", "complex"]
BackendName = Literal["qwen3-1.7b", "qwen3-4b"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a banking message.")
        return value


class RoutingDecision(StrictModel):
    """Jev decides difficulty only; the selected backend detects the intent."""

    route: Route
    confidence: float | None = Field(default=None, ge=0, le=1)


class BackendReply(StrictModel):
    intent: str = Field(min_length=1)
    reply: str
    policy_facts: list[str]


class Trace(StrictModel):
    router: Literal["mock_jev", "rule_router"]
    jev_choice: Route | None
    route: Route
    model: BackendName
    reply_source: Literal[
        "policy_template", "no_match", "unsupported_topic", "format_fallback"
    ]
    confidence: float | None = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    simulated_latency_ms: int = Field(ge=0)
    format_check: Literal["passed", "failed"]
    format_errors: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
    controller_action: str | None = None
    mocked: Literal[True] = True


class ChatResponse(StrictModel):
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    id: UUID
    intent: str
    reply: str = Field(min_length=1)
    policy_facts: list[str]
    trace: Trace


class LabelConfig(StrictModel):
    """One option that Jev and the Qwen backends classify a message into."""

    label: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # The original Banking77 category name, for mapping evaluation data;
    # None only for `unknown`.
    banking77_name: str | None
    description: str = Field(min_length=1)


class IntentConfig(StrictModel):
    intent: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    complexity: Route

    @field_validator("keywords")
    @classmethod
    def nonempty_keywords(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Keywords cannot be blank")
        return values


class PolicyCard(StrictModel):
    facts: list[str] = Field(min_length=2, max_length=3)
    reply_template: str = Field(min_length=1)

    @field_validator("reply_template")
    @classmethod
    def facts_placeholder(cls, value: str) -> str:
        if "{facts}" not in value:
            raise ValueError("Reply template must contain {facts}")
        if "{" in value.replace("{facts}", "") or "}" in value.replace("{facts}", ""):
            raise ValueError("Only the {facts} placeholder is supported")
        return value


class BackendConfig(StrictModel):
    name: BackendName
    route: Route
    latency_ms: int = Field(ge=0, le=5000)
    available: bool = True
    # Share of mock replies corrupted on purpose, to exercise the controller.
    fault_rate: float = Field(default=0, ge=0, le=1)
    # Share of mock calls that raise on purpose, to simulate an outage.
    error_rate: float = Field(default=0, ge=0, le=1)


class BackendMonitor(StrictModel):
    name: BackendName
    samples: int = Field(ge=0)
    format_failures: int = Field(ge=0)
    call_errors: int = Field(ge=0)
    failure_rate: float | None = Field(ge=0, le=1)
    disabled_by_controller: bool
    disabled_for_seconds: float | None = Field(default=None, ge=0)


class Intervention(StrictModel):
    at: datetime
    backend: BackendName
    action: Literal["disabled", "re-enabled", "kept_last_backend"]
    reason: str


class ControllerRules(StrictModel):
    window: int = Field(ge=1)
    min_samples: int = Field(ge=1)
    failure_threshold: float = Field(gt=0, le=1)
    cooldown_seconds: float = Field(gt=0)


class RecentRequest(StrictModel):
    # No request id: /v1/requests/{id} has no login, so listing ids would expose
    # other customers' replies.
    completed_at: datetime
    route: Route
    model: BackendName
    fallback_reason: str | None
    format_check: Literal["passed", "failed"]


class ModelUsage(StrictModel):
    name: BackendName
    attempted_calls: int = Field(ge=0)
    completed_calls: int = Field(ge=0)
    failed_calls: int = Field(ge=0)
    deadline_exceeded_calls: int = Field(default=0, ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


class JevBreakerStatus(StrictModel):
    """The Jev circuit breaker. While it is not closed, the rule router routes
    most or all messages."""

    state: Literal["closed", "open", "half_open"]
    consecutive_failures: int = Field(ge=0)
    failure_limit: int = Field(ge=1)
    cooldown_seconds: float = Field(gt=0)


class HealthResponse(StrictModel):
    recent_requests: list[RecentRequest] = Field(default_factory=list)
    usage: list[ModelUsage] = Field(default_factory=list)
    status: Literal["ok", "degraded"]
    mode: Literal["mock"] = "mock"
    router: Literal["mock_jev"] = "mock_jev"
    backends: list[BackendConfig]
    monitor: list[BackendMonitor]
    interventions: list[Intervention]
    controller: ControllerRules
    jev_breaker: JevBreakerStatus
