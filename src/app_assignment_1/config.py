"""Typed application configuration via Pydantic Settings.

Settings are read from environment variables (and, in local dev, an optional
`.env` file). Validation runs at import/startup time: if the environment is
misconfigured, the process fails fast and loudly with a clear error rather than
limping along with bad values.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, populated from the environment.

    Every knob a developer can turn lives here. See `.env.example` (local dev)
    and `.env.production.example` (deployed shape) for the paired templates.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # a typo'd key in .env is a mistake -> fail fast
    )

    # --- Runtime environment -------------------------------------------------
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False

    # --- HTTP server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Application ---------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Example secret-shaped knob: required in deployed environments, never
    # committed. Empty by default so local dev works without it.
    api_key: str = ""

    @field_validator("api_key")
    @classmethod
    def _require_api_key_in_production(cls, v: str, info) -> str:
        env = info.data.get("environment")
        if env == "production" and not v:
            raise ValueError(
                "APP_API_KEY must be set when APP_ENVIRONMENT=production"
            )
        return v

    @field_validator("debug")
    @classmethod
    def _no_debug_in_production(cls, v: bool, info) -> bool:
        if v and info.data.get("environment") == "production":
            raise ValueError("APP_DEBUG must be false when APP_ENVIRONMENT=production")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return the cached, validated settings singleton.

    Raises pydantic.ValidationError at first call if the environment is invalid,
    which surfaces at application startup.
    """
    return Settings()
