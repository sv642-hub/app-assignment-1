"""Exercise the vLLM client without a model server."""

import asyncio
import json
import math
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
            headers={"Authorization": "Bearer test-key"},
            base_url="http://vllm.test",
            transport=httpx.MockTransport(handler),
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

        assert request.headers["Authorization"] == "Bearer test-key"

        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "served-qwen"

        assert body["max_tokens"] == 512
        assert body["temperature"] == 0.0
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["logprobs"] is True

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

    reply = generate(handler)
    assert (
        BackendReply.model_validate(reply.model_dump(include=set(REPLY.model_fields)))
        == REPLY
    )
    # No logprobs in the response: the reply still counts, without a score.
    assert reply.label_confidence is None
    assert "enum" not in BackendReply.model_json_schema()["properties"]["intent"]


def test_http_503_is_a_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": MESSAGE})

    with pytest.raises(BackendCallError, match=r"^Backend returned HTTP 503$"):
        generate(handler)


def test_http_401_is_a_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    with pytest.raises(BackendCallError, match=r"^Backend returned HTTP 401$"):
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


def test_reply_carries_the_intent_token_confidence() -> None:
    content = REPLY.model_dump_json()
    start = content.index("card_arrival")
    pieces = [content[:start], "card", "_arrival", content[start + 12 :]]
    tokens = [
        {"token": pieces[0], "logprob": -5.0},
        {"token": pieces[1], "logprob": -0.1},
        {"token": pieces[2], "logprob": -0.2},
        {"token": pieces[3], "logprob": -7.0},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": content}, "logprobs": {"content": tokens}}
                ]
            },
        )

    reply = generate(handler)
    assert reply.label_confidence == pytest.approx(math.exp(-0.3))


def test_unreadable_logprobs_only_lose_the_score() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": REPLY.model_dump_json()},
                        "logprobs": {"content": "not a list"},
                    }
                ]
            },
        )

    reply = generate(handler)
    assert reply.intent == REPLY.intent
    assert reply.label_confidence is None


@pytest.mark.parametrize("bad", [1000, 0.5])
def test_positive_logprob_keeps_the_reply_and_loses_the_score(bad: float) -> None:
    content = REPLY.model_dump_json()
    start = content.index("card_arrival")
    tokens = [
        {"token": content[:start], "logprob": -0.1},
        {"token": "card_arrival", "logprob": bad},
        {"token": content[start + 12 :], "logprob": -0.1},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"content": content}}]}
        body["choices"][0]["logprobs"] = {"content": tokens}
        return httpx.Response(200, json=body)

    reply = generate(handler)
    # The validated reply still reaches the caller; only the score is lost.
    assert reply.intent == REPLY.intent
    assert reply.label_confidence is None
