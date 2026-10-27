"""HTTP client the Playground uses to reach the gateway.

Kept apart from the Streamlit page so it can be tested without Streamlit.
"""

from __future__ import annotations

import httpx
from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smart_gateway.schemas import ChatResponse


class PlaygroundSettings(BaseSettings):
    """Where the Playground finds the gateway. Read from PLAYGROUND_* variables."""

    model_config = SettingsConfigDict(env_prefix="PLAYGROUND_", extra="forbid")

    gateway_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    timeout_seconds: float = Field(default=10.0, gt=0)


def make_client(settings: PlaygroundSettings) -> httpx.Client:
    return httpx.Client(
        base_url=str(settings.gateway_url), timeout=settings.timeout_seconds
    )


def send_chat(http: httpx.Client, message: str) -> ChatResponse:
    """POST the message to /v1/chat and validate the reply."""
    response = http.post("/v1/chat", json={"message": message})
    response.raise_for_status()
    return ChatResponse.model_validate(response.json())
