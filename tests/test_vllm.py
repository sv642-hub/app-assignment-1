"""Exercise the vLLM client without a model server."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app_assignment_1.labels import load_labels
from app_assignment_1.schemas import BackendConfig, BackendReply
from app_assignment_1.vllm import (
    BackendCallError,
    BackendOutputError,
    BackendTimeoutError,
    VLLMBackend,
    build_system_prompt,
)

LABELS = load_labels(Path("config/labels.json"))
MESSAGE = "When will my new bank card arrive?"
REPLY = BackendReply(
    intent="card_arrival", reply="Within 5 business days.", policy_facts=[]
)


def generate(handler: Callable[[httpx.Request], httpx.Response]) -> BackendReply:
    async def run() -> BackendReply:
        config = BackendConfig(name="qwen3-1.7b", route="standard", latency_ms=0)
        async with httpx.AsyncClient(
            base_url="http://vllm.test", transport=httpx.MockTransport(handler)
        ) as client:
            backend = VLLMBackend(
                config,
                client,
                model="served-qwen",
                system_prompt=build_system_prompt(LABELS, {}),
                labels=LABELS,
            )
            return await backend.generate(MESSAGE)

    return asyncio.run(run())


def test_request_and_valid_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "served-qwen"
        system, user = body["messages"]
        assert system["role"] == "system"
        assert "Allowed intents:" in system["content"]
        assert user == {"role": "user", "content": MESSAGE}

        response_format = body["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert set(schema["required"]) == {"intent", "reply", "policy_facts"}
        assert len(schema["properties"]["intent"]["enum"]) == 78
        assert set(schema["properties"]["intent"]["enum"]) == LABELS.names
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": REPLY.model_dump_json()}}]},
        )

    assert generate(handler) == REPLY
    assert "enum" not in BackendReply.model_json_schema()["properties"]["intent"]


def test_http_503_is_a_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": MESSAGE})

    with pytest.raises(BackendCallError, match=r"^Backend returned HTTP 503$"):
        generate(handler)


@pytest.mark.parametrize(
    ("cause", "expected", "message"),
    [
        (httpx.ConnectError, BackendCallError, "Backend request failed"),
        (httpx.ReadTimeout, BackendTimeoutError, "Backend call timed out"),
    ],
)
def test_transport_failure(
    cause: type[httpx.RequestError], expected: type[Exception], message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise cause(MESSAGE, request=request)

    with pytest.raises(expected, match=f"^{message}$"):
        generate(handler)


@pytest.mark.parametrize(
    "content",
    [None, '{"intent": "card_arrival",', '{"intent": "card_arrival"}'],
    ids=["null", "truncated-json", "missing-fields"],
)
def test_invalid_completion_is_an_output_error(content: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    with pytest.raises(BackendOutputError, match=r"^Backend returned invalid output$"):
        generate(handler)
