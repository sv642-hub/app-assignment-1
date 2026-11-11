"""Backend interface and the mock Qwen backends: classify, read card, reply."""

import asyncio
import random
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
    "Could you describe the banking problem you need help with? "
    "Please rephrase your request with the details, or try an example. "
    "This is a fictional bank demo."
)


class Backend(Protocol):
    """A Qwen backend. A message that matches no intent must return intent
    `unknown`, no policy facts, and a reply asking the customer to describe
    their banking problem (#24). tests/test_beta_feedback.py checks the mock
    through the API; a real backend needs the same checks.
    """

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
        self.rng = random.Random()

    async def generate(self, message: str) -> BackendReply:
        await asyncio.sleep(self.config.latency_ms / 1000)
        if self.rng.random() < self.config.error_rate:
            raise ConnectionError("Simulated backend outage")
        _, match = match_intent(self.intents, message)
        if match is not None and self.rng.random() < self.config.label_swap_rate:
            # Simulated semantic fault: a confident reply for the wrong intent.
            # It passes the format check; only the semantic monitor sees it.
            match = self.intents[(self.intents.index(match) + 1) % len(self.intents)]
        if match is None:
            reply = BackendReply(intent="unknown", reply=UNKNOWN_REPLY, policy_facts=[])
        else:
            card = self.cards[match.intent]
            reply = BackendReply(
                intent=match.intent,
                reply=card.reply_template.format(facts=" ".join(card.facts)),
                policy_facts=card.facts,
            )
        if self.rng.random() < self.config.fault_rate:
            # Simulated fault: a template slot the model failed to fill.
            reply.reply += " {customer_name}"
        return reply
