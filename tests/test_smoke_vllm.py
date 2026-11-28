"""scripts/smoke_vllm.py: the vLLM key and the format check match the gateway."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_vllm.py"


def load():
    spec = importlib.util.spec_from_file_location("smoke_vllm", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("key", [" k-1", "k-1 ", "k-1\r", "k-1\r\n", "\tk-1\n"])
def test_the_key_is_stripped(key):
    assert load().clean_key(key) == "k-1"


@pytest.mark.parametrize("key", [None, "", "   ", "\r\n"])
def test_a_blank_key_sends_no_header(key):
    assert load().clean_key(key) is None


@pytest.mark.parametrize("key", ["secret 1", "secret\t1", "s\x00ecret", "sécret"])
def test_a_key_that_cannot_be_sent_is_refused_without_printing_it(key):
    with pytest.raises(SystemExit) as caught:
        load().clean_key(key)
    assert "ecret" not in str(caught.value)


def _config():
    from pydantic import TypeAdapter

    from app_assignment_1.labels import load_labels
    from app_assignment_1.schemas import PolicyCard

    root = SCRIPT.parents[1] / "config"
    cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
        (root / "policy_cards.json").read_text()
    )
    return load_labels(root / "labels.json"), cards


def _reply(intent="card_arrival", text="Soon.", facts=None):
    from app_assignment_1.schemas import BackendReply

    _, cards = _config()
    if facts is None:
        facts = cards["card_arrival"].facts[:1]
    return BackendReply(intent=intent, reply=text, policy_facts=facts)


def test_a_reply_the_gateway_accepts_passes():
    labels, cards = _config()
    assert load().check("standard", _reply(), labels, cards) == []


@pytest.mark.parametrize("text", ["", "Hi {name}", "x" * 4001])
def test_bad_model_text_is_only_noted_since_the_gateway_discards_it(text):
    # Before, these failed the smoke test though the gateway accepts them (#131).
    labels, cards = _config()
    notes = load().check("standard", _reply(text=text), labels, cards)
    assert len(notes) == 1 and "discarded" in notes[0]


@pytest.mark.parametrize(
    "reply,error",
    [
        ({"facts": ["a fact that is not on the card"]}, "unknown_policy_facts"),
        ({"facts": []}, "missing_policy_facts"),
        ({"intent": "lost_or_stolen_card"}, "expected card_arrival"),
    ],
)
def test_what_the_gateway_fails_still_fails(reply, error):
    labels, cards = _config()
    with pytest.raises(SystemExit, match=error):
        load().check("standard", _reply(**reply), labels, cards)
