"""Settings and config-file validation tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from smart_gateway.catalog import load_catalog
from smart_gateway.client import PlaygroundSettings
from smart_gateway.config import Settings

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_default_paths_point_at_config_dir() -> None:
    settings = Settings(_env_file=None)

    assert settings.intents_path == Path("config/intents.json")
    assert settings.backends_path.exists()


def test_missing_config_file_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_INTENTS_PATH", "config/nope.json")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_API_KEY", "")

    with pytest.raises(ValidationError, match="APP_API_KEY"):
        Settings(_env_file=None)


def test_every_intent_has_card_and_backend() -> None:
    catalog = load_catalog(
        CONFIG / "intents.json", CONFIG / "policy_cards.json", CONFIG / "backends.json"
    )

    assert set(catalog.cards) == {i.name for i in catalog.intents}
    assert {b.name for b in catalog.backends.values()} == {"qwen3-1.7b", "qwen3-4b"}


def test_intent_without_policy_card_rejected(tmp_path: Path) -> None:
    cards = json.loads((CONFIG / "policy_cards.json").read_text())
    cards["cards"] = cards["cards"][1:]
    (tmp_path / "cards.json").write_text(json.dumps(cards))

    with pytest.raises(ValueError, match="no policy card"):
        load_catalog(
            CONFIG / "intents.json", tmp_path / "cards.json", CONFIG / "backends.json"
        )


def test_route_without_backend_rejected(tmp_path: Path) -> None:
    backends = {"backends": [{"name": "small", "route": "standard", "latency_ms": 1}]}
    (tmp_path / "backends.json").write_text(json.dumps(backends))

    with pytest.raises(ValueError, match="no backend"):
        load_catalog(
            CONFIG / "intents.json",
            CONFIG / "policy_cards.json",
            tmp_path / "backends.json",
        )


def test_playground_reads_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYGROUND_GATEWAY_URL", "http://gateway:8000")

    assert str(PlaygroundSettings().gateway_url) == "http://gateway:8000/"
