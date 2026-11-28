import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from loguru import logger

from app_assignment_1.confidence import ScoredReply, label_confidence
from app_assignment_1.labels import LabelSet
from app_assignment_1.retry import retry_async
from app_assignment_1.schemas import BackendConfig, BackendReply, PolicyCard

# HTTP statuses that mean "try again": the server or a proxy in front of it is
# briefly unable, not that this request is wrong.
TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})


class BackendCallError(Exception):
    """The backend returned an HTTP error or the request failed.

    `transient` is True when repeating the call may succeed: a refused or
    reset connection, or a 429, 502, 503 or 504. `retry_after` is the seconds
    the server asked us to wait, when it said.
    """

    def __init__(
        self,
        message: str = "",
        *,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> None:
        """Set the message and the retry hints."""
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


class BackendTimeoutError(TimeoutError):
    """The backend call timed out. Repeating it is allowed."""

    transient = True
    retry_after = None


class BackendOutputError(Exception):
    """The backend returned an invalid completion."""


def is_transient(exc: Exception) -> bool:
    """Whether a failed call is worth repeating."""
    return bool(getattr(exc, "transient", False))


def _retry_after(exc: Exception) -> float | None:
    """The wait a server asked for, in seconds."""
    value = getattr(exc, "retry_after", None)
    return value if isinstance(value, float) else None


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header in its whole-seconds form. The HTTP
    date form and anything unreadable give None (back off as usual)."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds < float("inf") else None


def build_system_prompt(labels: LabelSet, cards: dict[str, PolicyCard]) -> str:
    label_text = "\n".join(
        f"- {item.label}: {item.description}" for item in labels.items
    )
    card_facts = json.dumps(
        {label: card.facts for label, card in cards.items()},
        ensure_ascii=False,
    )

    return (
        "You are a support assistant for a fictional bank.\n"
        "Choose exactly one intent from the list below. "
        "Choose unknown if the message is ambiguous or no label fits.\n"
        "Return one JSON object with intent, reply, and policy_facts.\n"
        "If the intent is unknown, ask the customer to describe their banking "
        "problem and return an empty policy_facts list.\n"
        "If the intent has a policy card, answer using only that card's facts. "
        "In policy_facts, list only the facts you used, copied from the card.\n"
        "For any other intent, return an empty reply and an empty "
        "policy_facts list.\n\n"
        f"Allowed intents:\n{label_text}\n\n"
        f"Policy card facts by intent:\n{card_facts}"
    )


class VLLMBackend:
    mocked = False

    def __init__(
        self,
        config: BackendConfig,
        client: httpx.AsyncClient,
        model: str,
        system_prompt: str,
        labels: LabelSet,
        retry_attempts: int = 1,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.retry_attempts = retry_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.sleep = sleep
        self.config = config
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.reply_schema = BackendReply.model_json_schema()
        self.reply_schema["properties"]["intent"]["enum"] = sorted(labels.names)

    async def ping(self) -> bool:
        """True if the vLLM server lists its models. No tokens are generated,
        so the readiness check can call it often."""
        try:
            response = await self.client.get("/v1/models", timeout=2)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _log_retry(self, attempt: int, exc: Exception, delay: float) -> None:
        """Say that a failed call will be repeated. Codes only, no text."""
        logger.warning(
            "Backend {} call {} failed ({}); retrying in {:.2f}s",
            self.config.name,
            attempt,
            type(exc).__name__,
            delay,
        )

    async def generate(self, message: str) -> BackendReply:
        """Ask the model for a reply, repeating the call after a transient
        failure (retry.py) and raising the typed error from the last try."""
        response = await retry_async(
            lambda: self._post(message),
            attempts=self.retry_attempts,
            base=self.retry_base_seconds,
            max_wait=self.retry_max_seconds,
            is_transient=is_transient,
            retry_after=_retry_after,
            on_retry=self._log_retry,
            sleep=self.sleep,
        )
        return self._parse(response)

    async def _post(self, message: str) -> httpx.Response:
        """One call to the server. HTTP and transport failures become
        BackendCallError or BackendTimeoutError."""
        try:
            response = await self.client.post(
                "/v1/chat/completions",
                json={
                    "model": self.model,
                    "max_tokens": 512,
                    "temperature": 0.0,
                    # Per-token log probabilities, for the intent's token
                    # confidence (confidence.py).
                    "logprobs": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": message},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "backend_reply",
                            "schema": self.reply_schema,
                        },
                    },
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise BackendTimeoutError("Backend call timed out") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise BackendCallError(
                f"Backend returned HTTP {status}",
                transient=status in TRANSIENT_STATUSES,
                retry_after=parse_retry_after(exc.response.headers.get("Retry-After")),
            ) from None
        except httpx.TransportError:
            # Refused, reset or dropped connection: nothing was answered.
            raise BackendCallError("Backend request failed", transient=True) from None
        except httpx.RequestError:
            raise BackendCallError("Backend request failed") from None
        return response

    def _parse(self, response: httpx.Response) -> BackendReply:
        """Read the reply, its intent confidence and its token counts."""
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
            reply = BackendReply.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackendOutputError("Backend returned invalid output") from None
        # A missing or unreadable logprobs block only loses the score.
        logprobs = choice.get("logprobs") if isinstance(choice, dict) else None
        tokens = logprobs.get("content") if isinstance(logprobs, dict) else None
        confidence = (
            label_confidence(content, tokens) if isinstance(tokens, list) else None
        )
        usage: Any = response.json().get("usage")
        return ScoredReply(
            **reply.model_dump(),
            label_confidence=confidence,
            prompt_tokens=_count(usage, "prompt_tokens"),
            completion_tokens=_count(usage, "completion_tokens"),
        )


def _count(usage: Any, key: str) -> int | None:
    """A token count from vLLM's usage block, or None if absent or odd."""
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None
