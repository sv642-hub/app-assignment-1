"""Fields for later analysis: the route order Jev saw, the settings version of
each request, keeping every request in Postgres, and the export script."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings, settings_version
from app_assignment_1.records import ROUTE_ORDER, JevAssessment
from app_assignment_1.router import MockJevRouter
from app_assignment_1.service import Gateway
from app_assignment_1.store import PostgresStore, create_store

MESSAGE = "my card has not arrived"
CONFIG_FILES = (
    Path("config/intents.json"),
    Path("config/labels.json"),
    Path("config/policy_cards.json"),
    Path("config/backends.json"),
)


def record(**overrides):
    values = {
        "request_id": uuid4(),
        "call_id": uuid4(),
        "question": "route",
        "status": "timeout",
        "error_type": "timeout",
        "finished_at": datetime(2026, 11, 22, tzinfo=UTC),
        "mocked": True,
        "option_set_version": "mock",
    } | overrides
    return JevAssessment(**values)


def test_the_route_order_is_checked():
    assert record(option_order=["complex", "standard"]).option_order == [
        "complex",
        "standard",
    ]
    assert record().option_order is None
    with pytest.raises(ValidationError):
        record(option_order=["standard", "standard"])
    with pytest.raises(ValidationError):
        record(option_order=["standard"])
    with pytest.raises(ValidationError):
        record(question="intent", option_order=["standard", "complex"])


def test_the_gateway_records_the_route_order():
    settings = Settings(_env_file=None)
    g = Gateway(settings, MockJevRouter(settings.intents_path))
    asyncio.run(g.chat(MESSAGE))
    saved = g.store.jev_assessments  # type: ignore[attr-defined]
    route, intent = sorted(saved, key=lambda r: r.question != "route")
    assert route.option_order == list(ROUTE_ORDER)
    assert intent.option_order is None


def test_the_settings_version_follows_settings_and_files_not_secrets(tmp_path):
    base = Settings(_env_file=None)
    first = settings_version(base, CONFIG_FILES)
    assert first.startswith("settings-")
    assert settings_version(Settings(_env_file=None), CONFIG_FILES) == first
    # A threshold or a switch changes it.
    assert (
        settings_version(
            Settings(_env_file=None, monitor_failure_threshold=0.6), CONFIG_FILES
        )
        != first
    )
    # A secret does not, so its value can't leak through the ID.
    keyed = base.model_copy(update={"jev_api_key": SecretStr("x" * 40)})
    assert settings_version(keyed, CONFIG_FILES) == first
    # A config file's content does.
    changed = tmp_path / "backends.json"
    backends = json.loads(Path("config/backends.json").read_text())
    backends[0]["latency_ms"] = 999
    changed.write_text(json.dumps(backends))
    files = (*CONFIG_FILES[:3], changed)
    assert settings_version(base, files) != first


def test_each_reply_and_health_carry_the_settings_version():
    settings = Settings(_env_file=None)
    with TestClient(create_app(settings)) as client:
        reply = client.post("/v1/chat", json={"message": MESSAGE}).json()
        health = client.get("/v1/health").json()
    assert reply["trace"]["settings_version"].startswith("settings-")
    assert health["settings_version"] == reply["trace"]["settings_version"]


def test_postgres_keeps_every_request_unless_a_limit_is_set():
    settings = Settings(
        _env_file=None, store="postgres", database_url="postgresql://u:p@h/db"
    )
    assert create_store(settings).limit is None  # type: ignore[attr-defined]
    limited = settings.model_copy(update={"postgres_request_limit": 50})
    assert create_store(limited).limit == 50  # type: ignore[attr-defined]


def load_export():
    spec = importlib.util.spec_from_file_location(
        "export_analysis", Path("scripts/export_analysis.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_writes_requests_and_assessments(postgres_url, tmp_path):
    settings = Settings(_env_file=None)
    store = PostgresStore(postgres_url, connect_timeout=10)
    g = Gateway(settings, MockJevRouter(settings.intents_path), store=store)

    async def go():
        await store.open()
        try:
            for _ in range(3):
                await g.chat(MESSAGE)
        finally:
            await store.close()

    asyncio.run(go())
    requests, assessments = load_export().export(postgres_url, tmp_path / "out")
    # Nothing was deleted: no limit is set.
    assert (requests, assessments) == (3, 6)
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "requests.jsonl").read_text().splitlines()
    ]
    assert {r["settings_version"] for r in rows} == {g.settings_version}
    assert "reply" not in rows[0] and "policy_facts" not in rows[0]
    records = [
        json.loads(line)
        for line in (tmp_path / "out" / "jev_assessments.jsonl")
        .read_text()
        .splitlines()
    ]
    route = [r for r in records if r["question"] == "route"]
    assert all(r["option_order"] == ["standard", "complex"] for r in route)
    assert {r["request_id"] for r in records} == {r["id"] for r in rows}


def test_a_fault_set_at_runtime_changes_the_settings_version():
    # Review of #99: /v1/faults changes the answers, so it must change the ID.
    token = "t" * 32
    auth = {"Authorization": f"Bearer {token}"}
    settings = Settings(_env_file=None, operator_token=token)
    with TestClient(create_app(settings)) as client:
        before = client.post("/v1/chat", json={"message": MESSAGE}).json()
        fault = {"model": "qwen3-1.7b", "fault": "label_swap"}
        assert client.post("/v1/faults", json=fault, headers=auth).status_code == 200
        during = client.post("/v1/chat", json={"message": MESSAGE}).json()
        health = client.get("/v1/health").json()
        clear = {"model": "qwen3-1.7b", "fault": "none"}
        assert client.post("/v1/faults", json=clear, headers=auth).status_code == 200
        after = client.post("/v1/chat", json={"message": MESSAGE}).json()
    first = before["trace"]["settings_version"]
    assert during["trace"]["settings_version"] != first
    assert health["settings_version"] == during["trace"]["settings_version"]
    # Back to the same config, back to the same ID.
    assert after["trace"]["settings_version"] == first


def test_the_settings_version_ignores_how_paths_are_spelled():
    relative = Settings(_env_file=None)
    absolute = Settings(
        _env_file=None,
        intents_path=relative.intents_path.resolve(),
        labels_path=relative.labels_path.resolve(),
        policy_cards_path=relative.policy_cards_path.resolve(),
        backends_path=relative.backends_path.resolve(),
    )
    rel = Gateway(relative, MockJevRouter(relative.intents_path))
    abs_ = Gateway(absolute, MockJevRouter(absolute.intents_path))
    assert rel.settings_version == abs_.settings_version


def test_a_password_in_a_url_does_not_change_the_settings_version():
    plain = Settings(_env_file=None, jev_base_url="https://api.typesafe.ai")
    versions = {
        settings_version(s, CONFIG_FILES)
        for s in (
            plain,
            Settings(_env_file=None, jev_base_url="https://u:one@api.typesafe.ai"),
            Settings(_env_file=None, jev_base_url="https://u:two@api.typesafe.ai"),
        )
    }
    assert len(versions) == 1


def test_a_blank_postgres_request_limit_keeps_everything(monkeypatch):
    monkeypatch.setenv("APP_POSTGRES_REQUEST_LIMIT", "")
    assert Settings(_env_file=None).postgres_request_limit is None
    monkeypatch.setenv("APP_POSTGRES_REQUEST_LIMIT", "500")
    assert Settings(_env_file=None).postgres_request_limit == 500
