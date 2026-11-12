import json

import httpx

from app_assignment_1.labels import LabelSet
from app_assignment_1.schemas import BackendConfig, BackendReply, PolicyCard


class BackendCallError(Exception):
    """The backend returned an unsuccessful HTTP status."""


class BackendTimeoutError(TimeoutError):
    """The backend call timed out."""


class BackendOutputError(Exception):
    """The backend returned an invalid completion."""


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
    def __init__(
        self,
        config: BackendConfig,
        client: httpx.AsyncClient,
        model: str,
        system_prompt: str,
        labels: LabelSet,
    ) -> None:
        self.config = config
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.reply_schema = BackendReply.model_json_schema()
        self.reply_schema["properties"]["intent"]["enum"] = sorted(labels.names)

    async def generate(self, message: str) -> BackendReply:
        try:
            response = await self.client.post(
                "/v1/chat/completions",
                json={
                    "model": self.model,
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
            raise BackendCallError(
                f"Backend returned HTTP {exc.response.status_code}"
            ) from None

        except httpx.RequestError:
            raise BackendCallError("Backend request failed") from None

        try:
            content = response.json()["choices"][0]["message"]["content"]
            return BackendReply.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackendOutputError("Backend returned invalid output") from None
