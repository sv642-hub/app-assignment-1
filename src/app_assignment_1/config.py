"""Cached Pydantic Settings. Environment variables override optional env files."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from psycopg.conninfo import conninfo_to_dict
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The throwaway password in env/compose.defaults.env. It is public, so a
# staging or production gateway refuses to start with it.
PUBLIC_DATABASE_PASSWORD = "smoke-test-only-not-for-deployment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=(".env", "env/.env"),
        env_file_encoding="utf-8",
        extra="forbid",
        # Validation errors must not print secrets such as APP_DATABASE_URL.
        hide_input_in_errors=True,
    )
    environment: Literal["local", "staging", "production"] = "local"
    intents_path: Path = Path("config/intents.json")
    labels_path: Path = Path("config/labels.json")
    policy_cards_path: Path = Path("config/policy_cards.json")
    backends_path: Path = Path("config/backends.json")
    request_store_limit: int = Field(default=1000, ge=1, le=100000)
    jev_timeout_seconds: float = Field(default=2, gt=0, le=30)
    rule_router_complex_min_words: int = Field(default=20, ge=1, le=2000)
    monitor_window: int = Field(default=20, ge=1, le=1000)
    monitor_min_samples: int = Field(default=5, ge=1, le=1000)
    monitor_failure_threshold: float = Field(default=0.5, gt=0, le=1)
    controller_cooldown_seconds: float = Field(default=60, gt=0, le=3600)
    # Semantic monitor (semantic.py). Placeholders until set from dev data.
    semantic_window: int = Field(default=50, ge=1, le=10000)
    semantic_min_samples: int = Field(default=10, ge=1, le=10000)
    semantic_min_coverage: float = Field(default=0.5, ge=0, le=1)
    # The disagreement rate of a healthy model. None: not measured yet, so the
    # monitor reports rates but never flags them.
    semantic_baseline_disagreement_rate: float | None = Field(default=None, ge=0, le=1)
    semantic_alert_margin: float = Field(default=0.15, ge=0, le=1)
    # Off: the semantic monitor only reports. On: the controller disables a
    # model above the baseline, with the same cooldown and action limit as for
    # format failures. Keep it off until the baseline comes from dev data.
    semantic_actions_enabled: bool = False
    # memory: process memory, lost on restart. postgres: needs database_url.
    store: Literal["memory", "postgres"] = "memory"
    database_url: SecretStr | None = None
    # Longest wait for one store write before the reply goes out without it.
    store_timeout_seconds: float = Field(default=2, gt=0, le=30)

    # Real providers (docs/JEV_QWEN_INTEGRATION_PLAN.md). Mock is the default;
    # real mode is rejected at startup until the clients exist.
    provider_mode: Literal["mock", "real"] = "mock"
    jev_base_url: AnyHttpUrl = AnyHttpUrl("https://api.typesafe.ai")
    jev_api_key: SecretStr | None = None
    jev_model: str = Field(default="jev-1.14.0", min_length=1)
    # USD per million input tokens; Jev does not charge for output tokens.
    # Leave unset when unknown, and cost stays null.
    jev_input_price_per_million_usd: float | None = Field(default=None, ge=0)
    jev_breaker_failures: int = Field(default=5, ge=1, le=1000)
    jev_breaker_cooldown_seconds: float = Field(default=30, gt=0, le=3600)
    request_deadline_seconds: float = Field(default=45, gt=0, le=600)
    alternate_reserve_seconds: float = Field(default=15, gt=0, le=600)

    @model_validator(mode="after")
    def min_samples_fit_window(self) -> "Settings":
        if self.monitor_min_samples > self.monitor_window:
            raise ValueError("monitor_min_samples cannot exceed monitor_window")
        if self.semantic_min_samples > self.semantic_window:
            raise ValueError("semantic_min_samples cannot exceed semantic_window")
        return self

    @model_validator(mode="after")
    def postgres_needs_url(self) -> "Settings":
        if self.database_url is not None and not self.database_url.get_secret_value():
            self.database_url = None
        if self.store == "postgres" and self.database_url is None:
            raise ValueError(
                "APP_DATABASE_URL is required when APP_STORE=postgres "
                "(for example postgresql://USER:PASSWORD@HOST:5432/DB)"
            )
        if self.database_url is not None:
            url = self.database_url.get_secret_value()
            try:
                conninfo_to_dict(url)
            except Exception:
                # The parser's message quotes the URL, password included.
                raise ValueError(
                    "APP_DATABASE_URL is not a valid Postgres URL "
                    "(for example postgresql://USER:PASSWORD@HOST:5432/DB)"
                ) from None
            if self.environment != "local" and PUBLIC_DATABASE_PASSWORD in url:
                raise ValueError(
                    "APP_DATABASE_URL uses the public password from "
                    "env/compose.defaults.env; set your own in env/.env and "
                    "env/postgres.env (README, Storage)"
                )
        return self

    @model_validator(mode="after")
    def deadline_leaves_time_for_both_attempts(self) -> "Settings":
        if (
            self.jev_timeout_seconds + self.alternate_reserve_seconds
            >= self.request_deadline_seconds
        ):
            raise ValueError(
                "request_deadline_seconds must exceed jev_timeout_seconds plus "
                "alternate_reserve_seconds, so the first attempt gets some time"
            )
        return self

    @model_validator(mode="after")
    def real_mode_needs_a_jev_key(self) -> "Settings":
        if self.provider_mode == "real" and (
            self.jev_api_key is None or not self.jev_api_key.get_secret_value()
        ):
            raise ValueError("provider_mode 'real' needs APP_JEV_API_KEY")
        return self

    @model_validator(mode="after")
    def real_mode_needs_https(self) -> "Settings":
        # The client sends the API key with every request.
        if self.provider_mode == "real" and self.jev_base_url.scheme != "https":
            raise ValueError("provider_mode 'real' needs an https APP_JEV_BASE_URL")
        return self


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process; restart after changing configuration."""
    return Settings()
