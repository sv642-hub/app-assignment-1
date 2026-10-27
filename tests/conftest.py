"""Shared fixtures: the real config/ catalog, with and without simulated latency."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_gateway.catalog import Catalog, load_catalog
from smart_gateway.config import get_settings

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(
        CONFIG / "intents.json",
        CONFIG / "policy_cards.json",
        CONFIG / "backends.json",
    )


@pytest.fixture
def fast_catalog(catalog: Catalog) -> Catalog:
    """The real catalog with backend latency set to zero."""
    for route, spec in catalog.backends.items():
        catalog.backends[route] = spec.model_copy(update={"latency_ms": 0})
    return catalog


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    # get_settings() is cached; reset it around each test so environment
    # overrides take effect and do not leak.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
