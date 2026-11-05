"""Probe skeleton: AUROC, training, the mock extractor and the gate decision."""

import math

import pytest
from pydantic import ValidationError

from app_assignment_1.probe import (
    LinearProbe,
    MockActivationExtractor,
    ProbeExample,
    auroc,
    run_gate,
    train_probe,
)


class LabelLeakExtractor:
    """Test double: real-looking activations that reveal the label."""

    mocked = False

    def extract(self, prompt: str, reply: str) -> list[float]:
        return [1.0 if prompt.startswith("good") else -1.0]


class MockLabelLeakExtractor(LabelLeakExtractor):
    mocked = True


def example(prompt: str, correct: bool, token_confidence: float) -> ProbeExample:
    return ProbeExample(
        prompt=prompt,
        reply="reply",
        correct=correct,
        token_confidence=token_confidence,
    )


def examples(
    good_confidence: float, bad_confidence: float, split: str = "train"
) -> list[ProbeExample]:
    return [
        example(f"good {split} 1", True, good_confidence),
        example(f"good {split} 2", True, good_confidence),
        example(f"bad {split} 1", False, bad_confidence),
        example(f"bad {split} 2", False, bad_confidence),
    ]


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([0.9, 0.8, 0.2, 0.1], 1.0),
        ([0.1, 0.2, 0.8, 0.9], 0.0),
        ([0.5, 0.5, 0.5, 0.5], 0.5),
        ([0.9, 0.2, 0.8, 0.1], 0.75),
        ([0.9, 0.5, 0.5, 0.1], 0.875),
    ],
)
def test_auroc(scores: list[float], expected: float) -> None:
    assert auroc(scores, [True, True, False, False]) == expected


@pytest.mark.parametrize("label", [True, False])
def test_auroc_needs_both_classes(label: bool) -> None:
    with pytest.raises(ValueError, match="both"):
        auroc([0.1, 0.2], [label, label])


def test_auroc_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        auroc([0.1], [True, False])


def test_mock_extractor_is_deterministic_and_bounded() -> None:
    extractor = MockActivationExtractor(size=4)
    features = extractor.extract("prompt", "reply")

    assert features == extractor.extract("prompt", "reply")
    assert features != extractor.extract("prompt", "another reply")
    assert len(features) == 4
    assert all(-1 <= value <= 1 for value in features)


def test_train_probe_separates_a_simple_signal() -> None:
    probe = train_probe([[1.0], [1.0], [-1.0], [-1.0]], [True, True, False, False])

    assert probe.score([1.0]) > 0.9
    assert probe.score([-1.0]) < 0.1


def test_train_probe_learns_a_bias() -> None:
    probe = train_probe([[2.0], [3.0], [2.5], [3.5]], [False, True, True, True])

    assert probe.bias != 0
    assert probe.score([3.5]) > probe.score([2.0])


def test_train_probe_weights_the_informative_feature() -> None:
    probe = train_probe(
        [[1.0, 0.3], [1.0, -0.3], [-1.0, 0.3], [-1.0, -0.3]],
        [True, True, False, False],
    )

    assert probe.weights[0] > 1
    assert abs(probe.weights[1]) < 0.01


def test_train_probe_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        train_probe([], [])
    with pytest.raises(ValueError, match="same length"):
        train_probe([[1.0]], [True, False])


def test_probe_score_stays_finite_on_extreme_logits() -> None:
    probe = LinearProbe(weights=(1.0,), bias=0.0)

    assert probe.score([-1000.0]) == 0.0
    assert probe.score([1000.0]) == 1.0


def test_probe_score_rejects_wrong_feature_size() -> None:
    with pytest.raises(ValueError):
        LinearProbe(weights=(1.0,), bias=0.0).score([1.0, 2.0])


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_probe_score_rejects_non_finite_features(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        LinearProbe(weights=(1.0,), bias=0.0).score([value])


def test_gate_passes_when_probe_beats_token_confidence() -> None:
    train = examples(0.5, 0.5)
    dev = examples(0.5, 0.5, split="dev")

    report = run_gate(train, dev, LabelLeakExtractor())

    assert report.probe_auroc == 1.0
    assert report.token_auroc == 0.5
    assert report.passed
    assert report.train_examples == 4
    assert report.dev_examples == 4
    assert not report.mocked


def test_gate_fails_on_a_tie() -> None:
    dev = examples(0.9, 0.1, split="dev")

    report = run_gate(examples(0.5, 0.5), dev, LabelLeakExtractor())

    assert report.probe_auroc == report.token_auroc == 1.0
    assert not report.passed


def test_gate_fails_when_token_confidence_is_better() -> None:
    train = examples(0.5, 0.5)
    dev = [
        example("bad dev 1", True, 0.9),
        example("good dev 1", False, 0.1),
    ]

    report = run_gate(train, dev, LabelLeakExtractor())

    assert report.probe_auroc == 0.0
    assert report.token_auroc == 1.0
    assert not report.passed


def test_mock_activations_never_pass_the_gate() -> None:
    dev = examples(0.5, 0.5, split="dev")

    report = run_gate(examples(0.5, 0.5), dev, MockLabelLeakExtractor())

    assert report.probe_auroc > report.token_auroc
    assert report.mocked
    assert not report.passed


def test_report_marks_the_hash_extractor_as_mocked() -> None:
    dev = examples(0.9, 0.1, split="dev")

    assert run_gate(examples(0.5, 0.5), dev, MockActivationExtractor()).mocked


def test_gate_rejects_prompts_in_both_sets() -> None:
    with pytest.raises(ValueError, match="share prompts"):
        run_gate(examples(0.5, 0.5), examples(0.5, 0.5), LabelLeakExtractor())


def test_example_rejects_confidence_outside_zero_to_one() -> None:
    with pytest.raises(ValidationError):
        example("good", True, 1.5)
