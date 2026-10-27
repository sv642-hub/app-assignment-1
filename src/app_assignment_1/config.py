"""Cached Pydantic Settings. Environment variables override optional env files."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process; restart after changing configuration."""
    return Settings()
