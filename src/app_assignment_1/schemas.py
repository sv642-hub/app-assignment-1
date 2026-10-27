"""Validated API contracts and on-disk mock configuration."""

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
    route: Route
    intent: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class Trace(StrictModel):
    router: Literal["mock_jev", "rule_router"]
    jev_choice: Route | None
    route: Route
    model: BackendName
    confidence: float = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    simulated_latency_ms: int = Field(ge=0)
    format_check: Literal["passed", "failed"]
    format_errors: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
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
