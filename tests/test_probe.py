"""Probe skeleton: AUROC, training, the mock extractor and the gate decision."""

import math
import random

import pytest
from pydantic import ValidationError

from app_assignment_1.probe import (
    MIN_DEV_CLASS_SIZE,
    GateRunInfo,
    LinearProbe,
    MockActivationExtractor,
    ProbeExample,
    auroc,
    auroc_difference_interval,
    run_gate,
    train_probe,
)

RUN_INFO = GateRunInfo(
    model="test-model",
    layer=0,
    pooling="last-token",
    token_confidence_method="test-method",
    dataset_version="test-data",
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
    good_confidence: float,
    bad_confidence: float,
    split: str = "train",
    count: int = 2,
) -> list[ProbeExample]:
    return [
        *[
            example(f"good {split} {i}", True, good_confidence)
            for i in range(1, count + 1)
        ],
        *[
            example(f"bad {split} {i}", False, bad_confidence)
            for i in range(1, count + 1)
        ],
    ]


def dev_examples(good_confidence: float, bad_confidence: float) -> list[ProbeExample]:
    return examples(good_confidence, bad_confidence, "dev", MIN_DEV_CLASS_SIZE)


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


def test_auroc_matches_comparing_every_pair() -> None:
    rng = random.Random(1)
    scores = [rng.randrange(10) / 10 for _ in range(60)]
    labels = [rng.random() < 0.4 for _ in range(60)]
    positives = [s for s, label in zip(scores, labels, strict=True) if label]
    negatives = [s for s, label in zip(scores, labels, strict=True) if not label]

    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positives
        for negative in negatives
    )

    assert auroc(scores, labels) == pytest.approx(
        wins / (len(positives) * len(negatives))
    )


def test_difference_interval_is_exact_when_scores_never_change_order() -> None:
    labels = [True, True, False, False]

    low, high = auroc_difference_interval(
        [0.9, 0.8, 0.2, 0.1], [0.5, 0.5, 0.5, 0.5], labels
    )

    assert (low, high) == (0.5, 0.5)


def test_difference_interval_is_zero_for_identical_scores() -> None:
    scores = [0.9, 0.2, 0.8, 0.1]

    low, high = auroc_difference_interval(scores, scores, [True, True, False, False])

    assert (low, high) == (0.0, 0.0)


def test_difference_interval_contains_the_observed_difference() -> None:
    rng = random.Random(2)
    labels = [index % 2 == 0 for index in range(80)]
    first = [rng.random() + 0.3 * label for label in labels]
    second = [rng.random() for _ in labels]

    low, high = auroc_difference_interval(first, second, labels)

    assert low < auroc(first, labels) - auroc(second, labels) < high
    assert low < high


def test_difference_interval_is_repeatable() -> None:
    scores = [0.9, 0.2, 0.8, 0.1, 0.4, 0.6]
    other = [0.3, 0.7, 0.5, 0.2, 0.9, 0.1]
    labels = [True, True, True, False, False, False]

    assert auroc_difference_interval(scores, other, labels) == (
        auroc_difference_interval(scores, other, labels)
    )


def test_difference_interval_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="same length"):
        auroc_difference_interval([0.1], [0.1, 0.2], [True, False])
    with pytest.raises(ValueError, match="both"):
        auroc_difference_interval([0.1, 0.2], [0.1, 0.2], [True, True])


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


@pytest.mark.parametrize("label", [True, False])
def test_train_probe_needs_both_classes(label: bool) -> None:
    with pytest.raises(ValueError, match="both"):
        train_probe([[1.0], [2.0]], [label, label])


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
    dev = dev_examples(0.5, 0.5)

    report = run_gate(train, dev, LabelLeakExtractor(), RUN_INFO)

    assert report.probe_auroc == 1.0
    assert report.token_auroc == 0.5
    assert report.passed
    assert report.difference_low == report.difference_high == 0.5
    assert report.train_examples == 4
    assert report.dev_examples == 2 * MIN_DEV_CLASS_SIZE
    assert report.run_info == RUN_INFO
    assert not report.mocked


def test_gate_fails_when_the_lead_could_be_noise() -> None:
    dev = [
        *[example(f"good right {i}", True, 0.5) for i in range(12)],
        *[example(f"bad right {i}", True, 0.5) for i in range(8)],
        *[example(f"good wrong {i}", False, 0.5) for i in range(8)],
        *[example(f"bad wrong {i}", False, 0.5) for i in range(12)],
    ]

    report = run_gate(examples(0.5, 0.5), dev, LabelLeakExtractor(), RUN_INFO)

    assert report.probe_auroc > report.token_auroc
    assert report.difference_low <= 0 < report.difference_high
    assert not report.passed


def test_gate_fails_on_a_tie() -> None:
    dev = dev_examples(0.9, 0.1)

    report = run_gate(examples(0.5, 0.5), dev, LabelLeakExtractor(), RUN_INFO)

    assert report.probe_auroc == report.token_auroc == 1.0
    assert not report.passed


def test_gate_fails_when_token_confidence_is_better() -> None:
    train = examples(0.5, 0.5)
    dev = [
        *[example(f"bad dev {i}", True, 0.9) for i in range(MIN_DEV_CLASS_SIZE)],
        *[example(f"good dev {i}", False, 0.1) for i in range(MIN_DEV_CLASS_SIZE)],
    ]

    report = run_gate(train, dev, LabelLeakExtractor(), RUN_INFO)

    assert report.probe_auroc == 0.0
    assert report.token_auroc == 1.0
    assert not report.passed


def test_mock_activations_never_pass_the_gate() -> None:
    dev = dev_examples(0.5, 0.5)

    report = run_gate(examples(0.5, 0.5), dev, MockLabelLeakExtractor(), RUN_INFO)

    assert report.probe_auroc > report.token_auroc
    assert report.mocked
    assert not report.passed


def test_report_marks_the_hash_extractor_as_mocked() -> None:
    dev = dev_examples(0.9, 0.1)

    assert run_gate(examples(0.5, 0.5), dev, MockActivationExtractor(), RUN_INFO).mocked


def test_gate_rejects_prompts_in_both_sets() -> None:
    with pytest.raises(ValueError, match="share prompts"):
        run_gate(examples(0.5, 0.5), examples(0.5, 0.5), LabelLeakExtractor(), RUN_INFO)


@pytest.mark.parametrize("prompt", ["GOOD TRAIN 1", "  good   train\t1 "])
def test_gate_ignores_case_and_whitespace_in_prompt_overlap(prompt: str) -> None:
    dev = [example(prompt, True, 0.5), example("bad dev 1", False, 0.5)]

    with pytest.raises(ValueError, match="share prompts"):
        run_gate(examples(0.5, 0.5), dev, LabelLeakExtractor(), RUN_INFO)


def test_gate_rejects_single_class_training_set() -> None:
    train = [example("good 1", True, 0.5), example("good 2", True, 0.5)]

    with pytest.raises(ValueError, match="both"):
        run_gate(train, dev_examples(0.5, 0.5), LabelLeakExtractor(), RUN_INFO)


@pytest.mark.parametrize("label", [True, False])
def test_gate_rejects_too_few_of_either_class(label: bool) -> None:
    dev = dev_examples(0.5, 0.5)
    dev.remove(next(item for item in dev if item.correct is label))

    with pytest.raises(ValueError, match="at least 20"):
        run_gate(examples(0.5, 0.5), dev, LabelLeakExtractor(), RUN_INFO)


@pytest.mark.parametrize(
    "change",
    [
        {"model": ""},
        {"layer": -1},
        {"pooling": ""},
        {"token_confidence_method": ""},
        {"dataset_version": ""},
    ],
)
def test_run_info_rejects_missing_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GateRunInfo.model_validate(RUN_INFO.model_dump() | change)


def test_example_rejects_confidence_outside_zero_to_one() -> None:
    with pytest.raises(ValidationError):
        example("good", True, 1.5)
