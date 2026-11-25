"""Cached Pydantic Settings. Environment variables override optional env files."""

import hashlib
import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict
from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The throwaway password in env/compose.defaults.env. It is public, so a
# staging or production gateway refuses to start with it.
PUBLIC_DATABASE_PASSWORD = "smoke-test-only-not-for-deployment"
# The operator token in compose.demo.yml; it is public.
PUBLIC_DEMO_OPERATOR_TOKEN = "chaos-demo-token-not-for-deployment"


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
    # Postgres keeps every request by default, for analysis. Set a number to
    # keep only the newest ones. The memory store uses request_store_limit.
    postgres_request_limit: int | None = Field(default=None, ge=1)

    @field_validator("postgres_request_limit", mode="before")
    @classmethod
    def blank_limit_keeps_everything(cls, value: object) -> object:
        # Uncommenting the template line and leaving it blank means "no limit".
        return None if isinstance(value, str) and not value.strip() else value

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
    # model above the baseline, with the same cooldown as for format failures,
    # but only while the other model is serving and has answered recently
    # without failing. A re-enable then also resets the semantic window. Keep
    # it off until the baseline comes from dev data.
    semantic_actions_enabled: bool = False
    # memory: process memory, lost on restart. postgres: needs database_url.
    store: Literal["memory", "postgres"] = "memory"
    database_url: SecretStr | None = None
    # Longest wait for one store write before the reply goes out without it.
    store_timeout_seconds: float = Field(default=2, gt=0, le=30)

    # Real providers (docs/JEV_QWEN_INTEGRATION_PLAN.md). Mock is the default.
    # jev_mode=real sends each message to TypeSafe. provider_mode=real sends it
    # to the two vLLM servers instead of the keyword mocks.
    jev_mode: Literal["mock", "real"] = "mock"
    provider_mode: Literal["mock", "real"] = "mock"
    jev_base_url: AnyHttpUrl = AnyHttpUrl("https://api.typesafe.ai")
    jev_api_key: SecretStr | None = None
    jev_model: str = Field(default="jev-1.14.0", min_length=1)
    # USD per million input tokens; Jev does not charge for output tokens.
    # Leave unset when unknown, and cost stays null. No inf or nan: 0 x inf is
    # nan, which would break /v1/health and /metrics.
    jev_input_price_per_million_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    jev_breaker_failures: int = Field(default=5, ge=1, le=1000)
    jev_breaker_cooldown_seconds: float = Field(default=30, gt=0, le=3600)
    # Bearer token for the operator routes (auth.py). Unset, empty or blank:
    # they answer 403. Set it only in the real env file, never in Git.
    operator_token: SecretStr | None = None
    # The two vLLM servers on the GPU host's private IP (compose.gpu.yml), for
    # example http://10.0.12.34:8001. Read only when provider_mode is real.
    vllm_small_base_url: AnyHttpUrl | None = None
    vllm_large_base_url: AnyHttpUrl | None = None
    # The key both vLLM servers require (VLLM_API_KEY in env/gpu.env), sent as a
    # bearer token. Server-side secret: set it only in the real env file.
    vllm_api_key: SecretStr | None = None
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
    def operator_token_is_long(self) -> "Settings":
        if self.operator_token is None:
            return self
        # The header value is stripped before the compare, so strip the token.
        value = self.operator_token.get_secret_value().strip()
        if not value:
            self.operator_token = None
        elif len(value) < 16:
            raise ValueError("APP_OPERATOR_TOKEN must be at least 16 characters")
        elif self.environment != "local" and value == PUBLIC_DEMO_OPERATOR_TOKEN:
            raise ValueError(
                "APP_OPERATOR_TOKEN is the public token from compose.demo.yml; "
                "stop applying compose.demo.yml and set your own in env/.env"
            )
        else:
            self.operator_token = SecretStr(value)
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
    def real_jev_needs_a_key_and_https(self) -> "Settings":
        if self.jev_mode != "real":
            return self
        if self.jev_api_key is None or not self.jev_api_key.get_secret_value():
            raise ValueError("jev_mode 'real' needs APP_JEV_API_KEY")
        # The client sends the API key with every request.
        if self.jev_base_url.scheme != "https":
            raise ValueError("jev_mode 'real' needs an https APP_JEV_BASE_URL")
        return self

    @model_validator(mode="after")
    def real_qwen_needs_both_urls_and_a_key(self) -> "Settings":
        if self.provider_mode != "real":
            return self
        if self.vllm_small_base_url is None or self.vllm_large_base_url is None:
            raise ValueError(
                "provider_mode 'real' needs APP_VLLM_SMALL_BASE_URL and "
                "APP_VLLM_LARGE_BASE_URL"
            )
        if self.vllm_api_key is None or not self.vllm_api_key.get_secret_value():
            raise ValueError("provider_mode 'real' needs APP_VLLM_API_KEY")
        return self


def settings_version(
    settings: "Settings",
    files: Iterable[Path],
    backends: Iterable[dict[str, Any]] | None = None,
) -> str:
    """A short ID for the settings and config that answer requests.

    Two requests with the same ID ran under the same thresholds, switches and
    config, so statistics can be grouped by it. What goes in:
    - the settings, except SecretStr fields (left out, not hashed) and the
      config file paths (their contents count, not how a path is spelled);
      a URL's user and password are dropped;
    - each file's name and contents;
    - `backends`, the models' live config, when given. The gateway passes it
      in place of backends.json, so a fault set at runtime changes the ID.
    """
    fields = type(settings).model_fields
    left_out = {
        name
        for name, field in fields.items()
        if field.annotation is Path
        or field.annotation is SecretStr
        or SecretStr in get_args(field.annotation)
    }
    values = {
        name: _without_userinfo(value) if isinstance(value, str) else value
        for name, value in settings.model_dump(mode="json").items()
        if name not in left_out
    }
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode())
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    if backends is not None:
        digest.update(json.dumps(list(backends), sort_keys=True).encode())
    return f"settings-{digest.hexdigest()[:12]}"


def _without_userinfo(value: str) -> str:
    """Drop a user and password from a URL, so they can't change the public ID."""
    parts = urlsplit(value)
    if not parts.scheme or "@" not in parts.netloc:
        return value
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]).geturl()


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process; restart after changing configuration."""
    return Settings()
