"""Mock model backends standing in for Qwen3-1.7B and Qwen3-4B on vLLM."""

from __future__ import annotations

import asyncio

from smart_gateway.catalog import BackendSpec, PolicyCard


class MockBackend:
    """Waits the configured latency, then fills the card's reply template."""

    def __init__(self, spec: BackendSpec) -> None:
        self.name = spec.name
        self.latency_ms = spec.latency_ms

    async def generate(self, message: str, card: PolicyCard) -> str:
        await asyncio.sleep(self.latency_ms / 1000)
        return card.reply_template.format(facts=" ".join(card.facts))
