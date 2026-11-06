"""The shared 78-label file, the uncarded-intent reply and the provider settings."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app_assignment_1.config import Settings
from app_assignment_1.intents import load_intents
from app_assignment_1.labels import UNKNOWN, load_labels
from app_assignment_1.router import MockJevRouter
from app_assignment_1.schemas import BackendConfig, BackendReply
from app_assignment_1.service import UNCARDED_REPLY, Gateway

LABELS = Path("config/labels.json")


def test_label_file_has_the_77_banking77_intents_plus_unknown():
    labels = load_labels(LABELS)
    assert len(labels.items) == 78
    assert UNKNOWN in labels.names
    assert sum(item.banking77_name is not None for item in labels.items) == 77
    carded = {item.intent for item in load_intents(Path("config/intents.json"))}
    assert carded <= labels.names


def test_option_set_version_ignores_formatting_but_not_content(tmp_path):
    items = json.loads(LABELS.read_text())
    same = tmp_path / "same.json"
    same.write_text(json.dumps(items))
    changed = tmp_path / "changed.json"
    items[0]["description"] += " Changed."
    changed.write_text(json.dumps(items))
    version = load_labels(LABELS).option_set_version
    assert version.startswith("labels-")
    assert load_labels(same).option_set_version == version
    assert load_labels(changed).option_set_version != version


@pytest.mark.parametrize(
    "edit",
    [
        lambda items: items + [items[0]],
        lambda items: [i for i in items if i["label"] != UNKNOWN],
        lambda items: [{**items[0], "banking77_name": None}] + items[1:],
        lambda items: [{**items[0], "label": "Has Spaces"}] + items[1:],
    ],
    ids=["duplicate", "no_unknown", "missing_banking77_name", "bad_label"],
)
def test_bad_label_files_fail(tmp_path, edit):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(edit(json.loads(LABELS.read_text()))))
    with pytest.raises(ValueError):
        load_labels(path)


def test_carded_intent_missing_from_labels_fails_startup(tmp_path):
    items = [i for i in json.loads(LABELS.read_text()) if i["label"] != "card_arrival"]
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(items))
    settings = Settings(_env_file=None, labels_path=path)
    with pytest.raises(ValueError, match="policy card"):
        Gateway(settings, MockJevRouter(settings.intents_path))


class FixedBackend:
    def __init__(self, name, route, reply):
        self.config = BackendConfig(name=name, route=route, latency_ms=0)
        self.reply = reply

    async def generate(self, message):
        return self.reply


def chat_with(reply: BackendReply):
    settings = Settings(_env_file=None)
    backends = [
        FixedBackend("qwen3-1.7b", "standard", reply),
        FixedBackend("qwen3-4b", "complex", reply),
    ]
    gateway = Gateway(settings, MockJevRouter(settings.intents_path), backends)
    return asyncio.run(gateway.chat("How old do I need to be?"))


@pytest.mark.parametrize("generated", ["", "Ignored {customer_name} text."])
def test_uncarded_intent_gets_the_fixed_reply(generated):
    result = chat_with(
        BackendReply(intent="age_limit", reply=generated, policy_facts=[])
    )
    assert result.intent == "age_limit"
    assert result.reply == UNCARDED_REPLY
    assert result.policy_facts == []
    assert result.trace.reply_source == "unsupported_topic"
    assert result.trace.format_check == "passed"


def test_uncarded_intent_with_policy_facts_fails_format_check():
    result = chat_with(
        BackendReply(intent="age_limit", reply="", policy_facts=["Invented fact."])
    )
    assert result.intent == UNKNOWN
    assert result.policy_facts == []
    assert result.trace.reply_source == "format_fallback"
    assert result.trace.format_errors == ["uncarded_policy_facts"]


def test_empty_reply_for_a_carded_intent_still_fails():
    result = chat_with(BackendReply(intent="card_arrival", reply="", policy_facts=[]))
    assert result.trace.format_errors == ["empty_reply"]


@pytest.mark.parametrize("path", ["env/.env.example", "env/.env.production.example"])
def test_env_templates_parse(path):
    settings = Settings(_env_file=path)
    assert settings.provider_mode == "mock"
    assert settings.jev_model == "jev-1.14.0"
    assert settings.jev_api_key is None


def test_real_mode_needs_a_key():
    with pytest.raises(ValidationError, match="APP_JEV_API_KEY"):
        Settings(_env_file=None, provider_mode="real")
    with pytest.raises(ValidationError, match="APP_JEV_API_KEY"):
        Settings(_env_file=None, provider_mode="real", jev_api_key="")


def test_real_mode_is_rejected_until_the_clients_exist():
    settings = Settings(_env_file=None, provider_mode="real", jev_api_key="test-key")
    assert "test-key" not in repr(settings)
    with pytest.raises(ValueError, match="not built yet"):
        Gateway(settings, MockJevRouter(settings.intents_path))


@pytest.mark.parametrize(
    "values",
    [
        {"request_deadline_seconds": 17, "alternate_reserve_seconds": 15},
        {"request_deadline_seconds": 10, "alternate_reserve_seconds": 15},
    ],
)
def test_deadline_must_leave_time_for_the_first_attempt(values):
    with pytest.raises(ValidationError, match="request_deadline_seconds"):
        Settings(_env_file=None, **values)
