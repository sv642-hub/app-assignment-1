"""Logging: JSON lines, the request id, the access line, and no private text."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app_assignment_1.api import create_app
from app_assignment_1.confidence import ScoredReply
from app_assignment_1.config import Settings
from app_assignment_1.logs import add_tokens, annotate, configure_logging, scrub

SECRET_MESSAGE = "my card number is 4111-1111-1111-1111 please help"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(_env_file=None, backends_path=path)


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """The JSON log lines written to stderr so far."""
    err = capsys.readouterr().err
    return [json.loads(x) for x in err.splitlines() if x.startswith("{")]


def test_every_line_is_json_with_level_and_message(capsys) -> None:
    configure_logging("INFO", "json")
    logger.info("hello {}", "world")
    [entry] = lines(capsys)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert "time" in entry


def test_level_filters(capsys) -> None:
    configure_logging("WARNING", "json")
    logger.info("quiet")
    logger.warning("loud")
    assert [e["message"] for e in lines(capsys)] == ["loud"]


def test_text_format_for_people(capsys) -> None:
    configure_logging("INFO", "text")
    with logger.contextualize(request_id="abc123"):
        logger.info("hi")
    err = capsys.readouterr().err
    assert "[abc123] hi" in err
    assert not err.lstrip().startswith("{")


def test_scrub_drops_private_field_names() -> None:
    kept = scrub(
        {"model": "m", "Message": "x", "reply": "y", "api_key": "k", "status": 200}
    )
    assert kept == {"model": "m", "status": 200}


def test_a_bound_secret_never_reaches_the_line(capsys) -> None:
    configure_logging("INFO", "json")
    logger.bind(reply="private reply", password="hunter2", model="m").info("x")
    [entry] = lines(capsys)
    assert entry["model"] == "m"
    assert "private reply" not in json.dumps(entry)
    assert "hunter2" not in json.dumps(entry)


def test_exception_is_logged_by_type_only(capsys) -> None:
    configure_logging("INFO", "json")
    try:
        raise ValueError("card 4111 leaked in the message")
    except ValueError:
        logger.opt(exception=True).error("failed")
    [entry] = lines(capsys)
    assert entry["error_type"] == "ValueError"
    assert "4111" not in json.dumps(entry)


def test_stdlib_logging_goes_through_loguru(capsys) -> None:
    import logging

    configure_logging("INFO", "json")
    logging.getLogger("some.library").warning("from stdlib")
    [entry] = lines(capsys)
    assert entry["message"] == "from stdlib"
    assert entry["stdlib_logger"] == "some.library"


def test_annotate_and_tokens_are_ignored_outside_a_request() -> None:
    annotate(model="x")
    add_tokens(1, 2)


def test_request_gets_an_id_in_header_and_log(settings, capsys) -> None:
    with TestClient(create_app(settings)) as client:
        configure_logging("INFO", "json")
        response = client.post("/v1/chat", json={"message": "card arrival"})
    rid = response.headers["X-Request-ID"]
    assert len(rid) == 32
    [entry] = [e for e in lines(capsys) if e["message"] == "request completed"]
    assert entry["request_id"] == rid
    assert entry["method"] == "POST"
    assert entry["endpoint"] == "/v1/chat"
    assert entry["status"] == 200
    assert entry["duration_ms"] >= 0
    assert entry["model"] == "qwen3-1.7b"


def test_a_safe_client_id_is_kept_and_an_unsafe_one_replaced(settings) -> None:
    with TestClient(create_app(settings)) as client:
        kept = client.get("/v1/health", headers={"X-Request-ID": "trace-42.a_b"})
        replaced = client.get("/v1/health", headers={"X-Request-ID": "bad id\t{x}"})
    assert kept.headers["X-Request-ID"] == "trace-42.a_b"
    assert replaced.headers["X-Request-ID"] != "bad id\t{x}"
    assert len(replaced.headers["X-Request-ID"]) == 32


def test_error_responses_carry_the_id_too(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/chat", json={})
    assert response.status_code == 422
    assert "X-Request-ID" in response.headers


def test_no_log_line_has_the_customers_message_or_the_reply(settings, capsys) -> None:
    with TestClient(create_app(settings)) as client:
        configure_logging("DEBUG", "json")
        response = client.post("/v1/chat", json={"message": SECRET_MESSAGE})
    reply = response.json()["reply"]
    err = capsys.readouterr().err
    assert "4111" not in err
    assert SECRET_MESSAGE not in err
    assert reply not in err


def test_token_counts_from_the_model_reach_the_access_line(settings, capsys) -> None:
    scored = ScoredReply(
        intent="unknown",
        reply="Please describe your banking problem.",
        policy_facts=[],
        prompt_tokens=120,
        completion_tokens=17,
    )

    async def generate(message: str) -> ScoredReply:
        return scored

    with TestClient(create_app(settings)) as client:
        configure_logging("INFO", "json")
        for backend in client.app.state.gateway.backends:
            backend.generate = generate
        client.post("/v1/chat", json={"message": "hello"})
    [entry] = [e for e in lines(capsys) if e["message"] == "request completed"]
    assert entry["prompt_tokens"] == 120
    assert entry["completion_tokens"] == 17
