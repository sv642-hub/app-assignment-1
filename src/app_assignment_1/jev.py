"""The real Jev client: one TypeSafe System One call with two Choice questions.

`route` picks standard or complex; `intent` picks one of the 78 labels. Each
answer is checked on its own, so a bad intent answer does not lose the route.
A failure of the whole call raises `JevCallError`, and the gateway falls back to
the rule router. Not wired into the app yet: APP_PROVIDER_MODE=real is still
rejected, because the Qwen clients and the real-mode trace fields (plan step C5)
are not built.

TypeSafe docs: https://docs.typesafe.ai/primitives/choice and
https://docs.typesafe.ai/api/errors
"""

import math
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app_assignment_1.config import Settings
from app_assignment_1.labels import LabelSet
from app_assignment_1.records import ROUTE_OPTIONS, check_probabilities
from app_assignment_1.router import JevAnswer, JevResult

SYSTEM_ONE_PATH = "/v1/systemone"
REQUEST_ID_HEADER = "x-typesafe-request-id"

ROUTE_INSTRUCTIONS = "How much care does this banking support message need?"
ROUTE_CRITERIA = {
    "standard": (
        "A routine request: a status check, a fee or rate question, or how-to "
        "steps. No money is missing, and there is no fraud or dispute."
    ),
    "complex": (
        "Money that is missing, wrong or charged twice; a lost, stolen or "
        "compromised card; a dispute or refund; or several problems in one "
        "message."
    ),
}
INTENT_INSTRUCTIONS = "Which banking support request is the customer making?"

# Status codes TypeSafe documents. Others are grouped as http_5xx or http_other.
DOCUMENTED_STATUSES = frozenset({400, 401, 413, 415, 422, 429, 529})


class JevCallError(Exception):
    """The whole Jev call failed. `error_type` is a short code; the message
    never holds provider text, the customer's message or the API key."""

    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


class JevTimeout(JevCallError, TimeoutError):
    """The HTTP client gave up waiting. The gateway counts it as jev_timeout."""


class _Usage(BaseModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class _SystemOneResponse(BaseModel):
    # Extra top-level fields are ignored, so a new provider field does not fail
    # every call.
    model: str = Field(min_length=1, strict=True)
    answers: dict[str, Any]
    usage: _Usage


def build_questions(labels: LabelSet) -> dict[str, Any]:
    """The two Choice questions. Intent options keep the label file's order,
    which is part of `option_set_version`."""
    return {
        "route": {
            "type": "choice",
            "instructions": ROUTE_INSTRUCTIONS,
            "criteria": ROUTE_CRITERIA,
        },
        "intent": {
            "type": "choice",
            "instructions": INTENT_INSTRUCTIONS,
            "criteria": {item.label: item.description for item in labels.items},
        },
    }


def create_jev_http_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """One shared client for the app's lifetime. The caller closes it.

    The gateway's routing deadline is the real limit; this timeout is a backstop
    so an abandoned connection does not linger. No retries, and no redirects,
    so the key is only ever sent to the configured host.
    """
    if settings.jev_api_key is None or not settings.jev_api_key.get_secret_value():
        raise ValueError("The Jev client needs APP_JEV_API_KEY")
    return httpx.AsyncClient(
        base_url=str(settings.jev_base_url),
        headers={"Authorization": f"Bearer {settings.jev_api_key.get_secret_value()}"},
        timeout=httpx.Timeout(settings.jev_timeout_seconds),
        follow_redirects=False,
        transport=transport,
    )


class TypeSafeJevRouter:
    """Implements `Router` with one real Jev call per message."""

    mocked = False

    def __init__(self, client: httpx.AsyncClient, model: str, labels: LabelSet):
        self.option_set_version = labels.option_set_version
        self.client = client
        self.model = model
        self.questions = build_questions(labels)
        self.intent_options = frozenset(item.label for item in labels.items)

    async def route(self, message: str) -> JevResult:
        body = {"model": self.model, "state": message, "questions": self.questions}
        try:
            response = await self.client.post(SYSTEM_ONE_PATH, json=body)
        except httpx.TimeoutException:
            raise JevTimeout("timeout") from None
        except httpx.RequestError:
            raise JevCallError("request_failed") from None
        if response.status_code != 200:
            raise JevCallError(_status_code(response.status_code))
        try:
            payload = response.json()
        except ValueError:
            raise JevCallError("bad_json") from None
        try:
            parsed = _SystemOneResponse.model_validate(payload)
        except ValidationError:
            raise JevCallError("bad_body") from None
        return JevResult(
            route=_answer(parsed.answers.get("route"), ROUTE_OPTIONS),
            intent=_answer(parsed.answers.get("intent"), self.intent_options),
            mocked=False,
            jev_model=parsed.model,
            provider_request_id=response.headers.get(REQUEST_ID_HEADER) or None,
            input_tokens=parsed.usage.input_tokens,
            output_tokens=parsed.usage.output_tokens,
        )


def _status_code(status: int) -> str:
    if status in DOCUMENTED_STATUSES:
        return f"http_{status}"
    return "http_5xx" if status >= 500 else "http_other"


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _answer(raw: Any, options: frozenset[str]) -> JevAnswer:
    """Check one Choice answer. A bad answer becomes `error_type` only."""
    if raw is None:
        return JevAnswer(error_type="missing_answer")
    if not isinstance(raw, dict) or raw.get("type") != "choice":
        return JevAnswer(error_type="wrong_type")
    choice: Any = raw.get("choice")
    confidence: Any = raw.get("confidence")
    probabilities: Any = raw.get("probabilities")
    if (
        not isinstance(choice, str)
        or not _is_number(confidence)
        or not isinstance(probabilities, dict)
        or not all(_is_number(value) for value in probabilities.values())
    ):
        return JevAnswer(error_type="malformed_answer")
    if set(probabilities) != options:
        return JevAnswer(error_type="wrong_options")
    if not (math.isfinite(confidence) and 0 <= confidence <= 1):
        return JevAnswer(error_type="invalid_confidence")
    try:
        check_probabilities(choice, probabilities)
    except ValueError:
        return JevAnswer(error_type="invalid_probabilities")
    return JevAnswer(
        choice=choice,
        confidence=float(confidence),
        probabilities={key: float(value) for key, value in probabilities.items()},
    )
