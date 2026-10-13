"""Settings tests for the language-model configuration."""

import pytest
from pydantic import ValidationError

from app_assignment_1.config import Settings


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("APP_LLM_MAX_NEW_TOKENS", raising=False)


def test_llm_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_model_name == "HuggingFaceTB/SmolLM2-135M-Instruct"
    assert settings.llm_max_new_tokens == 64


def test_llm_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LLM_MODEL_NAME", "example/other-model")
    monkeypatch.setenv("APP_LLM_MAX_NEW_TOKENS", "128")

    settings = Settings(_env_file=None)

    assert settings.llm_model_name == "example/other-model"
    assert settings.llm_max_new_tokens == 128


@pytest.mark.parametrize("value", [1, 512])
def test_max_new_tokens_accepts_bounds(value: int) -> None:
    settings = Settings(_env_file=None, llm_max_new_tokens=value)

    assert settings.llm_max_new_tokens == value


@pytest.mark.parametrize("value", ["0", "-5", "513"])
def test_max_new_tokens_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("APP_LLM_MAX_NEW_TOKENS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_model_name_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LLM_MODEL_NAME", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
