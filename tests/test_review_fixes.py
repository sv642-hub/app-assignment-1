"""Regression tests for format validation and the router boundary."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.service import check_reply


@pytest.mark.parametrize(
    "reply,errors",
    [
        ("  ", ["empty_reply"]),
        ("a" * 4001, ["reply_too_long"]),
        ("Hello {customer_name}", ["unfilled_placeholder"]),
        ("a" * 4000, []),
        ("Use the app. A set can be written {1, 2}.", []),
    ],
)
def test_format_boundaries(reply, errors):
    assert check_reply(reply) == errors


@pytest.mark.parametrize(
    "fact,reason",
    [
        (" ", "empty_reply"),
        ("x" * 4001, "reply_too_long"),
        ("Hello {customer_name}", "unfilled_placeholder"),
    ],
)
def test_invalid_generated_reply_is_replaced_and_traced(tmp_path, fact, reason):
    cards = json.loads(Path("config/policy_cards.json").read_text())
    cards["card_arrival"] = {"facts": [fact, fact], "reply_template": "{facts}"}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(cards))
    with TestClient(
        create_app(Settings(_env_file=None, policy_cards_path=path))
    ) as client:
        result = client.post("/v1/chat", json={"message": "card arrival"}).json()
        assert result["trace"]["format_check"] == "failed"
        assert reason in result["trace"]["format_errors"]
        assert result["reply"].startswith("Sorry,")
        assert result["policy_facts"] == []
        assert client.get(f"/v1/requests/{result['id']}").json() == result


@pytest.mark.parametrize(
    "error", [RuntimeError, ValueError, TimeoutError, ConnectionError]
)
def test_router_errors_fall_back_without_logging_sensitive_details(error, caplog):
    class BrokenRouter:
        async def route(self, message):
            raise error("secret-provider-detail")

    with TestClient(create_app(Settings(_env_file=None), BrokenRouter())) as client:
        response = client.post("/v1/chat", json={"message": "private-message"})
        assert response.status_code == 200
        assert response.json()["trace"]["fallback_reason"] == "router_unavailable"
    assert "Router failed" in caplog.text
    assert "secret-provider-detail" not in caplog.text
    assert "private-message" not in caplog.text


def test_router_cancellation_is_not_swallowed():
    from app_assignment_1.service import Gateway

    class CancelledRouter:
        async def route(self, message):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Gateway(Settings(_env_file=None), CancelledRouter()).chat("card"))
