"""Backend interface and the mock Qwen backends: classify, read card, reply."""

import asyncio
import random
from typing import Protocol

from app_assignment_1.confidence import ScoredReply
from app_assignment_1.intents import match_intent
from app_assignment_1.schemas import (
    BackendConfig,
    BackendReply,
    IntentConfig,
    PolicyCard,
)

# Fixed token confidence for the mock models, which have no token
# probabilities: a keyword match is sure and no match is unsure. A swapped
# label stays sure, because the fault it simulates is a confident wrong
# answer. Enough to exercise escalation; it measures nothing.
MOCK_CONFIDENCE_MATCH = 0.9
MOCK_CONFIDENCE_NO_MATCH = 0.3

UNKNOWN_REPLY = (
    "I'm sorry, I could not match that message to a demo banking topic. "
    "Could you describe the banking problem you need help with? "
    "Please rephrase your request with the details, or try an example. "
    "This is a fictional bank demo."
)
# The fact the mock bad_format fault adds. No policy card lists it.
INVENTED_FACT = "DemoBank waives every fee forever."


class Backend(Protocol):
    """A Qwen backend. A message that matches no intent must return intent
    `unknown`, no policy facts, and a reply asking the customer to describe
    their banking problem (#24). tests/test_beta_feedback.py checks the mock
    through the API; a real backend needs the same checks.
    """

    config: BackendConfig
    # True for a simulated model. The semantic monitor only compares a mock
    # with mock Jev, or a real model with real Jev (see semantic.py).
    mocked: bool

    async def generate(self, message: str) -> BackendReply:
        """Answer one message with this backend's intent and reply."""


class MockBackend:
    """Both mock models share one keyword classifier; only the delay differs."""

    mocked = True

    def __init__(
        self,
        config: BackendConfig,
        intents: list[IntentConfig],
        cards: dict[str, PolicyCard],
    ):
        """Keep the backend config, the intents to match and the policy cards."""
        self.config = config
        self.intents = intents
        self.cards = cards
        self.rng = random.Random()

    async def generate(self, message: str) -> BackendReply:
        # The call runs on the config it started with, as a real call runs on
        # the version it was sent to, even if a release changes it meanwhile.
        """Match the message to an intent and fill that intent's card."""
        config = self.config
        if self.rng.random() < config.hang_rate:
            # Simulated hang: no answer until the caller gives up.
            await asyncio.sleep(3600)
        await asyncio.sleep(config.latency_ms / 1000)
        # The new faults draw a random number only when set, so the existing
        # ones keep the same sequence.
        if config.slow_rate and self.rng.random() < config.slow_rate:
            # Simulated slow or hung model: the gateway's deadline may cut it.
            await asyncio.sleep(config.slow_seconds)
        if self.rng.random() < config.error_rate:
            raise ConnectionError("Simulated backend outage")
        _, match = match_intent(self.intents, message)
        if config.bad_prompt_rate and self.rng.random() < config.bad_prompt_rate:
            # Simulated bad prompt: a well-formed reply with no intent.
            match = None
        if match is not None and self.rng.random() < config.label_swap_rate:
            # Simulated semantic fault: a confident reply for the wrong intent.
            # It passes the format check; only the semantic monitor sees it.
            match = self.intents[(self.intents.index(match) + 1) % len(self.intents)]
        if match is None:
            reply = ScoredReply(
                intent="unknown",
                reply=UNKNOWN_REPLY,
                policy_facts=[],
                label_confidence=MOCK_CONFIDENCE_NO_MATCH,
            )
        else:
            card = self.cards[match.intent]
            reply = ScoredReply(
                intent=match.intent,
                reply=card.reply_template.format(facts=" ".join(card.facts)),
                policy_facts=card.facts,
                label_confidence=MOCK_CONFIDENCE_MATCH,
            )
        if self.rng.random() < config.fault_rate:
            # Simulated fault: a policy fact the model made up. It is on no card,
            # so the reply fails the format check (unknown_policy_facts).
            reply.policy_facts = [*reply.policy_facts, INVENTED_FACT]
        return reply
