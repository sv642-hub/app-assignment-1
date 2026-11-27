"""ModelLensExtractor with a fake lens and tokenizer: no torch needed."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from pydantic import TypeAdapter

from app_assignment_1 import probe_modellens
from app_assignment_1.config import get_settings
from app_assignment_1.labels import load_labels
from app_assignment_1.probe import (
    ActivationExtractor,
    ProbeExample,
    train_probe,
)
from app_assignment_1.probe_modellens import ModelLensExtractor, gateway_system_prompt
from app_assignment_1.schemas import BackendConfig, PolicyCard
from app_assignment_1.vllm import BackendCallError, VLLMBackend, build_system_prompt


class FakeVector:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class FakeTokens:
    def __init__(self, text):
        self.text = text
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    def __init__(self, chat_template=None):
        self.chat_template = chat_template

    def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kwargs):
        # ChatML, the format Qwen3 uses.
        assert tokenize is False
        text = "".join(
            f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages
        )
        if add_generation_prompt:
            text += "<|im_start|>assistant\n"
        return text

    def __call__(self, text, add_special_tokens):
        # One "token" per character, so ids join back into the text.
        assert add_special_tokens is False
        return {"input_ids": list(text)}


def fake_batch(ids):
    return FakeTokens("".join(ids))


class FakeActivation:
    """Stands in for a [1, tokens, hidden] tensor."""

    def __init__(self, rows):
        self.rows = rows


def fake_pool(activation, pooling):
    rows = activation.rows
    if pooling == "last":
        vector = rows[-1]
    else:
        vector = [sum(col) / len(rows) for col in zip(*rows, strict=True)]
    return [FakeVector(vector)]


class FakeLens:
    """One row per character, so the reply changes the activations."""

    def __init__(self, layers=28):
        self.names = [f"model.layers.{i}" for i in range(layers)]
        self.attached = []
        self.last = None
        self.ran = None

    def layer_names(self):
        return self.names

    def attach_layers(self, names):
        self.attached = names
        return self

    def run(self, tokens):
        self.ran = tokens
        self.last = FakeActivation([[float(ord(c)), 1.0] for c in tokens.text])

    def get_layer_activation(self, name):
        return self.last if name in self.attached else None


def test_extractor_reads_the_named_layer():
    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), layer=14, pool=fake_pool, to_batch=fake_batch
    )
    assert extractor.layer_name == "model.layers.14"
    assert extractor.mocked is False


def test_extractor_fits_the_protocol():
    extractor: ActivationExtractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), layer=0
    )
    assert extractor.mocked is False


def test_unknown_layer_is_rejected():
    with pytest.raises(ValueError, match="not found"):
        ModelLensExtractor(FakeLens(layers=2), FakeTokenizer(), layer=5)


@pytest.mark.parametrize(
    ("layer", "pooling"), [(-1, "last"), (0, "max")], ids=["layer", "pooling"]
)
def test_bad_settings_are_rejected(layer, pooling):
    with pytest.raises(ValueError):
        ModelLensExtractor(FakeLens(), FakeTokenizer(), layer=layer, pooling=pooling)


def test_last_pooling_reads_the_final_token():
    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), layer=3, pool=fake_pool, to_batch=fake_batch
    )
    assert extractor.extract("hi", "ok") == [float(ord("k")), 1.0]


def test_mean_pooling_averages_tokens():
    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), 3, "mean", pool=fake_pool, to_batch=fake_batch
    )
    # "a\nc": codes 97, 10, 99.
    assert extractor.extract("a", "c") == [(97 + 10 + 99) / 3, 1.0]


def test_chat_template_matches_what_the_gateway_sent():
    extractor = ModelLensExtractor(
        FakeLens(),
        FakeTokenizer("t"),
        0,
        pool=fake_pool,
        to_batch=fake_batch,
        system_prompt="rules",
    )
    assert extractor.format_text("q", '{"intent": "x"}') == (
        "<|im_start|>system\nrules<|im_end|>\n"
        "<|im_start|>user\nq<|im_end|>\n"
        '<|im_start|>assistant\n{"intent": "x"}'
    )


def test_last_pooling_lands_on_the_reply_not_the_template():
    extractor = ModelLensExtractor(
        FakeLens(),
        FakeTokenizer("t"),
        0,
        pool=fake_pool,
        to_batch=fake_batch,
        system_prompt="rules",
    )
    # The fake lens makes one row per character; the last is the reply's "}".
    assert extractor.extract("q", '{"intent": "x"}') == [float(ord("}")), 1.0]


def test_prompt_and_reply_are_tokenized_separately():
    seen = []
    tokenizer = FakeTokenizer("t")
    call = tokenizer.__call__

    class Recording(FakeTokenizer):
        def __call__(self, text, add_special_tokens):
            seen.append(text)
            return call(text, add_special_tokens)

    recording = Recording("t")
    extractor = ModelLensExtractor(
        FakeLens(),
        recording,
        0,
        pool=fake_pool,
        to_batch=fake_batch,
        system_prompt="rules",
    )
    prefix, reply = extractor.format_parts("q", "{}")
    assert reply == "{}"
    assert prefix.endswith("<|im_start|>assistant\n")
    assert extractor.token_ids("q", "{}") == list(prefix + reply)
    assert seen == [prefix, "{}"]


def test_no_system_prompt_without_one():
    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), 0, pool=fake_pool, to_batch=fake_batch
    )
    assert extractor.format_text("q", "a") == "q\na"


def test_gateway_system_prompt_lists_every_label():
    prompt = gateway_system_prompt()
    assert prompt.startswith("You are a support assistant")
    for label in load_labels(get_settings().labels_path).names:
        assert f"- {label}:" in prompt


def test_tokens_move_to_the_device():
    lens = FakeLens()
    extractor = ModelLensExtractor(
        lens,
        FakeTokenizer(),
        layer=0,
        device="cuda",
        pool=fake_pool,
        to_batch=fake_batch,
    )
    extractor.extract("q", "a")
    assert lens.ran.device == "cuda"


def test_non_finite_activations_raise():
    def nan_pool(activation, pooling):
        return [FakeVector([float("nan")])]

    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), 0, pool=nan_pool, to_batch=fake_batch
    )
    with pytest.raises(ValueError, match="finite"):
        extractor.extract("q", "a")


def test_missing_activation_raises():
    lens = FakeLens()
    extractor = ModelLensExtractor(
        lens, FakeTokenizer(), layer=0, pool=fake_pool, to_batch=fake_batch
    )
    lens.get_layer_activation = lambda name: None
    with pytest.raises(RuntimeError, match="No activation"):
        extractor.extract("q", "a")


def test_default_pool_explains_a_missing_extra(monkeypatch):
    def fake_import(name, *args):
        raise ImportError(name)

    monkeypatch.setattr(probe_modellens.importlib, "import_module", fake_import)
    extractor = ModelLensExtractor(FakeLens(), FakeTokenizer(), layer=0)
    with pytest.raises(ImportError, match="--extra probe"):
        extractor.extract("q", "a")


def test_features_train_a_probe():
    extractor = ModelLensExtractor(
        FakeLens(), FakeTokenizer(), layer=0, pool=fake_pool, to_batch=fake_batch
    )
    examples = [
        ProbeExample(prompt=f"m{i}", reply=r, correct=r == "z", token_confidence=0.5)
        for i in range(4)
        for r in ("a", "z")
    ]
    features = [extractor.extract(e.prompt, e.reply) for e in examples]
    probe = train_probe(features, [e.correct for e in examples])
    assert len(probe.weights) == 2


def test_from_pretrained_explains_a_missing_extra(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, *args):
        if name in {"modellens", "transformers"}:
            raise ImportError(name)
        return real_import(name, *args)

    monkeypatch.setattr(probe_modellens.importlib, "import_module", fake_import)
    with pytest.raises(ImportError, match="--extra probe"):
        ModelLensExtractor.from_pretrained(layer=0)


def backend_request() -> dict:
    """The JSON body the vLLM backend sends, captured without a server."""
    settings = get_settings()
    labels = load_labels(settings.labels_path)
    cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
        settings.policy_cards_path.read_text()
    )
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(500)

    async def run() -> None:
        config = BackendConfig(name="qwen3-1.7b", route="standard", latency_ms=0)
        async with httpx.AsyncClient(
            base_url="http://vllm.test", transport=httpx.MockTransport(handler)
        ) as client:
            backend = VLLMBackend(
                config,
                client,
                model="qwen3-1.7b",
                system_prompt=build_system_prompt(labels, cards),
                labels=labels,
            )
            with pytest.raises(BackendCallError):
                await backend.generate("q")

    asyncio.run(run())
    return sent


def test_system_prompt_is_byte_equal_to_what_the_backend_sends():
    system = backend_request()["messages"][0]
    assert system["role"] == "system"
    assert system["content"] == gateway_system_prompt()


def test_template_kwargs_match_what_the_backend_sends():
    sent = backend_request().get("chat_template_kwargs", {})
    assert probe_modellens.CHAT_TEMPLATE_KWARGS == sent


def test_default_revision_is_the_one_vllm_serves():
    compose = yaml.safe_load(Path("compose.gpu.yml").read_text())
    command = compose["services"]["vllm-small"]["command"]
    assert "--model=Qwen/Qwen3-1.7B" in command
    assert f"--revision={probe_modellens.DEFAULT_REVISION}" in command
    assert probe_modellens.DEFAULT_MODEL == "Qwen/Qwen3-1.7B"


def test_from_pretrained_uses_the_gateway_defaults(monkeypatch):
    loaded: dict = {}

    class FakeModel:
        def to(self, device):
            loaded["device"] = device
            return self

    class Auto:
        def __init__(self, result):
            self.result = result

        def from_pretrained(self, name, revision, **kwargs):
            loaded.setdefault("calls", []).append((name, revision))
            return self.result

    tokenizer = FakeTokenizer()
    transformers = SimpleNamespace(
        AutoTokenizer=Auto(tokenizer), AutoModelForCausalLM=Auto(FakeModel())
    )

    class FakeAdapter:
        def set_tokenizer(self, value):
            loaded["tokenizer"] = value

    def fake_lens(model, backend):
        lens = FakeLens()
        lens.adapter = FakeAdapter()
        return lens

    modules = {
        "transformers": transformers,
        "modellens": SimpleNamespace(ModelLens=fake_lens),
    }
    monkeypatch.setattr(probe_modellens.importlib, "import_module", modules.__getitem__)

    extractor = ModelLensExtractor.from_pretrained(layer=3)
    expected = ("Qwen/Qwen3-1.7B", probe_modellens.DEFAULT_REVISION)
    assert loaded["calls"] == [expected, expected]
    assert loaded["device"] == "cpu"
    assert loaded["tokenizer"] is tokenizer
    assert extractor.system_prompt == gateway_system_prompt()

    no_system = ModelLensExtractor.from_pretrained(layer=3, system_prompt="")
    assert no_system.system_prompt is None
