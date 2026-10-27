"""Backend interface and the mock Qwen backends: classify, read card, reply."""

import asyncio
from typing import Protocol

from app_assignment_1.intents import match_intent
from app_assignment_1.schemas import (
    BackendConfig,
    BackendReply,
    IntentConfig,
    PolicyCard,
)

UNKNOWN_REPLY = (
    "I'm sorry, I could not match that message to a demo banking topic. "
    "Please rephrase it or try an example. This is a fictional bank demo."
)


class Backend(Protocol):
    config: BackendConfig

    async def generate(self, message: str) -> BackendReply: ...


class MockBackend:
    """Both mock models share one keyword classifier; only the delay differs."""

    def __init__(
        self,
        config: BackendConfig,
        intents: list[IntentConfig],
        cards: dict[str, PolicyCard],
    ):
        self.config = config
        self.intents = intents
        self.cards = cards

    async def generate(self, message: str) -> BackendReply:
        await asyncio.sleep(self.config.latency_ms / 1000)
        _, match = match_intent(self.intents, message)
        if match is None:
            return BackendReply(intent="unknown", reply=UNKNOWN_REPLY, policy_facts=[])
        card = self.cards[match.intent]
        return BackendReply(
            intent=match.intent,
            reply=card.reply_template.format(facts=" ".join(card.facts)),
            policy_facts=card.facts,
        )
