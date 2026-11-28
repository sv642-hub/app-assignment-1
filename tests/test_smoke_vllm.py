"""scripts/smoke_vllm.py sends the vLLM key as the gateway does (#132)."""

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
