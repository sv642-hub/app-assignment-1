"""Validated API contracts and on-disk mock configuration."""

from datetime import datetime
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
    reply_source: Literal["policy_template", "no_match", "format_fallback"]
    confidence: float | None = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    simulated_latency_ms: int = Field(ge=0)
    format_check: Literal["passed", "failed"]
    format_errors: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
    controller_action: str | None = None
    mocked: Literal[True] = True


class ChatResponse(StrictModel):
    id: UUID
    intent: str
    reply: str = Field(min_length=1)
    policy_facts: list[str]
    trace: Trace


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


class BackendMonitor(StrictModel):
    name: BackendName
    samples: int = Field(ge=0)
    format_failures: int = Field(ge=0)
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


class HealthResponse(StrictModel):
    status: Literal["ok", "degraded"]
    mode: Literal["mock"] = "mock"
    router: Literal["mock_jev"] = "mock_jev"
    backends: list[BackendConfig]
    monitor: list[BackendMonitor]
    interventions: list[Intervention]
    controller: ControllerRules
