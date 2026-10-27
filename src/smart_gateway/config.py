"""Typed application configuration via Pydantic Settings.

Settings are read from environment variables (and, in local dev, an optional
`.env` file). Validation runs at import/startup time: if the environment is
misconfigured, the process fails fast and loudly with a clear error rather than
limping along with bad values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, FilePath, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, populated from the environment.

    Every knob a developer can turn lives here. See `env/.env.example` (local
    dev) and `env/.env.production.example` (deployed shape) for the paired
    templates.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # a typo'd key in .env is a mistake -> fail fast
    )

    # Runtime environment
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False

    # HTTP server
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # Application
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_key: str = ""

    # Config files that stand in for Jev and the models. Relative paths resolve
    # against the working directory: the repo root locally, /app in Docker.
    intents_path: FilePath = Path("config/intents.json")
    policy_cards_path: FilePath = Path("config/policy_cards.json")
    backends_path: FilePath = Path("config/backends.json")

    # How many recent requests GET /v1/requests/{id} can look up.
    request_store_size: int = Field(default=1000, gt=0)

    @field_validator("api_key")
    @classmethod
    def _require_api_key_in_production(cls, v: str, info) -> str:
        """Require a non-empty API key when running in production."""
        env = info.data.get("environment")
        if env == "production" and not v:
            raise ValueError("APP_API_KEY must be set when APP_ENVIRONMENT=production")
        return v

    @field_validator("debug")
    @classmethod
    def _no_debug_in_production(cls, v: bool, info) -> bool:
        """Forbid debug mode in production to avoid leaking internals."""
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
