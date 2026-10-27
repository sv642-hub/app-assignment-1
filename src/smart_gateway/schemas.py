"""Request and response models shared by the gateway API and the Playground."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smart_gateway import __version__
from smart_gateway.catalog import Route

FormatCheck = Literal["pass", "fail"]
FallbackReason = Literal["no_intent_match", "router_error"]


class ChatRequest(BaseModel):
    """A customer message sent to the gateway."""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="The customer's message.",
        examples=["I lost my card yesterday, what should I do?"],
    )


class PolicyCardRef(BaseModel):
    """The policy card a reply was grounded in."""

    intent: str
    title: str
    facts: list[str]


class Trace(BaseModel):
    """How the gateway handled one request."""

    router: str = Field(description='"mock-jev", or "rule-router" on fallback.')
    route: Route = Field(description="Jev's choice: standard or complex.")
    confidence: float = Field(ge=0.0, le=1.0)
    model: str | None = Field(description="Backend that answered; null on fallback.")
    latency_ms: float = Field(ge=0.0, description="Gateway time for the request.")
    format_check: FormatCheck
    fallback_reason: FallbackReason | None = None


class ChatResponse(BaseModel):
    """The gateway's answer to one message, with its trace."""

    request_id: str
    created_at: datetime
    message: str
    intent: str
    reply: str
    policy_card: PolicyCardRef | None
    trace: Trace


class HealthResponse(BaseModel):
    """Small typed payload confirming the service is up."""

    status: Literal["ok"] = "ok"
    version: str = __version__
    environment: str
    router: str
    intents: int
    backends: list[str]


class ErrorResponse(BaseModel):
    """Error payload for 404 and 503 responses."""

    detail: str
