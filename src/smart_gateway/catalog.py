"""Load and validate the JSON config files in config/.

Each file is parsed into a Pydantic model, then checked against the others so a
missing policy card or backend stops startup instead of failing on a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Route = Literal["standard", "complex"]


class IntentSpec(BaseModel):
    """One Banking77 intent the mock router can recognize."""

    name: str = Field(min_length=1)
    complexity: Route
    keywords: list[str] = Field(min_length=1)


class IntentsFile(BaseModel):
    intents: list[IntentSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> IntentsFile:
        names = [intent.name for intent in self.intents]
        if len(names) != len(set(names)):
            raise ValueError("intent names must be unique")
        return self


class PolicyCard(BaseModel):
    """The facts a correct reply for one intent must contain."""

    intent: str = Field(min_length=1)
    title: str = Field(min_length=1)
    facts: list[str] = Field(min_length=1)
    reply_template: str = Field(pattern=r"\{facts\}")


class PolicyCardsFile(BaseModel):
    bank_name: str = Field(min_length=1)
    fallback_reply: str = Field(min_length=1)
    cards: list[PolicyCard] = Field(min_length=1)


class BackendSpec(BaseModel):
    """A mock model server and the route it serves."""

    name: str = Field(min_length=1)
    route: Route
    latency_ms: int = Field(ge=0)


class BackendsFile(BaseModel):
    backends: list[BackendSpec] = Field(min_length=1)


@dataclass(frozen=True)
class Catalog:
    """Everything the gateway reads from config/, indexed for lookups."""

    intents: list[IntentSpec]
    cards: dict[str, PolicyCard]
    fallback_reply: str
    backends: dict[Route, BackendSpec]


def load_catalog(
    intents_path: Path, policy_cards_path: Path, backends_path: Path
) -> Catalog:
    """Parse the three config files and check that they agree."""
    intents = IntentsFile.model_validate_json(intents_path.read_text()).intents
    cards_file = PolicyCardsFile.model_validate_json(policy_cards_path.read_text())
    backends = BackendsFile.model_validate_json(backends_path.read_text()).backends

    cards = {card.intent: card for card in cards_file.cards}
    missing_cards = [i.name for i in intents if i.name not in cards]
    if missing_cards:
        raise ValueError(f"no policy card for intents: {missing_cards}")

    by_route: dict[Route, BackendSpec] = {}
    for backend in backends:
        if backend.route in by_route:
            raise ValueError(f"more than one backend serves route {backend.route!r}")
        by_route[backend.route] = backend
    missing_routes = {i.complexity for i in intents} - by_route.keys()
    if missing_routes:
        raise ValueError(f"no backend for routes: {sorted(missing_routes)}")

    return Catalog(
        intents=intents,
        cards=cards,
        fallback_reply=cards_file.fallback_reply,
        backends=by_route,
    )
