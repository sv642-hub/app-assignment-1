"""Cached Pydantic Settings. Environment variables override optional env files."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=(".env", "env/.env"),
        env_file_encoding="utf-8",
        extra="forbid",
    )
    environment: Literal["local", "staging", "production"] = "local"
    intents_path: Path = Path("config/intents.json")
    policy_cards_path: Path = Path("config/policy_cards.json")
    backends_path: Path = Path("config/backends.json")
    gateway_url: str = "http://127.0.0.1:8000"
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    request_store_limit: int = Field(default=1000, ge=1, le=100000)
    jev_timeout_seconds: float = Field(default=2, gt=0, le=30)
    rule_router_complex_min_words: int = Field(default=20, ge=1, le=2000)
    monitor_window: int = Field(default=20, ge=1, le=1000)
    monitor_min_samples: int = Field(default=5, ge=1, le=1000)
    monitor_failure_threshold: float = Field(default=0.5, gt=0, le=1)
    controller_cooldown_seconds: float = Field(default=60, gt=0, le=3600)

    @model_validator(mode="after")
    def min_samples_fit_window(self) -> "Settings":
        if self.monitor_min_samples > self.monitor_window:
            raise ValueError("monitor_min_samples cannot exceed monitor_window")
        return self


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process; restart after changing configuration."""
    return Settings()
