"""Offline linear probe skeleton: do activations predict a correct output?

The probe scores each model output from Qwen3-1.7B activations. The gate in
docs/TEAM_PLAN.md compares that score with the model's own token-probability
confidence by AUROC on the dev set. Nothing here runs in the gateway.

Only `MockActivationExtractor` exists so far, so results from it say nothing
about the real model. See docs/PROBE.md for what is still to build.
"""

import hashlib
import math
import random
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Protocol, Self

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app_assignment_1.schemas import StrictModel

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
BOOTSTRAP_TAIL = 0.025  # 2.5% in each tail gives a 95% interval.

# Below this, the interval is overconfident. See docs/PROBE.md.
MIN_DEV_CLASS_SIZE = 20

CHANCE_AUROC = 0.5

# Strict fields reject look-alike inputs: True for 1, 1.0 for an int, "no" for
# False. Strings are stripped first, so whitespace alone counts as empty.
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProbeExample(StrictModel):
    """One model output. `correct` is the label the probe learns to predict.

    The proposed label is the predicted intent matching gold (docs/PROBE.md).

    `token_confidence` is a probability, so a mean log probability must be
    exponentiated first. That does not change the order of scores, or AUROC.
    """

    prompt: str = Field(min_length=1)
    reply: str
    correct: bool = Field(strict=True)
    token_confidence: float = Field(ge=0, le=1, strict=True)

    @field_validator("prompt")
    @classmethod
    def _prompt_has_text(cls, prompt: str) -> str:
        # The prompt is kept as given, because the extractor reads it.
        if not normalize_prompt(prompt):
            raise ValueError("prompt must contain visible text.")
        return prompt


class GateRunInfo(StrictModel):
    """What a gate run measured, so a saved report can be cited and repeated."""

    model: NonBlankStr
    layer: int = Field(ge=0, strict=True)
    pooling: NonBlankStr
    token_confidence_method: NonBlankStr
    dataset_version: NonBlankStr


def gate_passes(mocked: bool, difference_low: float, probe_auroc_low: float) -> bool:
    """The pass rule: real activations, a lead over token confidence, and
    a probe that beats chance on its own. See docs/PROBE.md."""
    return not mocked and difference_low > 0 and probe_auroc_low > CHANCE_AUROC


class GateReport(StrictModel):
    """A gate result. It rejects values that contradict the pass rule, so a
    saved report loaded with `model_validate_json` is consistent too. It is
    frozen, so a field cannot be changed after the check runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_auroc: float = Field(ge=0, le=1, strict=True)
    probe_auroc_low: float = Field(ge=0, le=1, strict=True)
    probe_auroc_high: float = Field(ge=0, le=1, strict=True)
    token_auroc: float = Field(ge=0, le=1, strict=True)
    difference_low: float = Field(ge=-1, le=1, strict=True)
    difference_high: float = Field(ge=-1, le=1, strict=True)
    train_examples: int = Field(ge=2, strict=True)
    dev_examples: int = Field(ge=2, strict=True)
    passed: bool = Field(strict=True)
    mocked: bool = Field(strict=True)
    run_info: GateRunInfo

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.difference_low > self.difference_high:
            raise ValueError("difference_low must not exceed difference_high.")
        if self.probe_auroc_low > self.probe_auroc_high:
            raise ValueError("probe_auroc_low must not exceed probe_auroc_high.")
        expected = gate_passes(self.mocked, self.difference_low, self.probe_auroc_low)
        if self.passed != expected:
            raise ValueError(
                "passed must equal: not mocked, difference_low > 0 and "
                f"probe_auroc_low > {CHANCE_AUROC}."
            )
        return self


class ActivationExtractor(Protocol):
    """Turns a prompt and reply into one fixed-length activation vector."""

    mocked: bool

    def extract(self, prompt: str, reply: str) -> list[float]: ...


class MockActivationExtractor:
    """Deterministic hash features: they carry no signal about correctness."""

    mocked = True

    def __init__(self, size: int = 8):
        self.size = size

    def extract(self, prompt: str, reply: str) -> list[float]:
        features = []
        for index in range(self.size):
            text = f"{index}\0{prompt}\0{reply}".encode()
            byte = hashlib.sha256(text).digest()[0]
            features.append(byte / 127.5 - 1)
        return features


@dataclass(frozen=True)
class LinearProbe:
    weights: tuple[float, ...]
    bias: float

    def logit(self, features: Sequence[float]) -> float:
        """Log odds that the output is correct. Use this to rank outputs:
        `score` rounds to exactly 0.0 or 1.0 once the logit passes about 37,
        which would turn different outputs into ties."""
        logit = self.bias + sum(
            weight * value for weight, value in zip(self.weights, features, strict=True)
        )
        if not math.isfinite(logit):
            raise ValueError("Probe features must be finite numbers.")
        return logit

    def score(self, features: Sequence[float]) -> float:
        """Estimated probability that the output is correct."""
        return _sigmoid(self.logit(features))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1 + exponent)


def train_probe(
    features: Sequence[Sequence[float]],
    labels: Sequence[bool],
    steps: int = 500,
    learning_rate: float = 0.5,
) -> LinearProbe:
    """Fit logistic regression by gradient descent.

    Placeholder trainer in plain Python, to be replaced (see docs/PROBE.md).
    """
    if not features:
        raise ValueError("Training needs at least one example.")
    if len(features) != len(labels):
        raise ValueError("Features and labels must have the same length.")
    if len(set(labels)) < 2:
        raise ValueError("Training needs both correct and wrong examples.")

    weights = [0.0] * len(features[0])
    bias = 0.0
    for _ in range(steps):
        probe = LinearProbe(tuple(weights), bias)
        weight_gradient = [0.0] * len(weights)
        bias_gradient = 0.0
        for row, label in zip(features, labels, strict=True):
            error = probe.score(row) - float(label)
            bias_gradient += error
            for index, value in enumerate(row):
                weight_gradient[index] += error * value
        step = learning_rate / len(features)
        weights = [
            weight - step * gradient
            for weight, gradient in zip(weights, weight_gradient, strict=True)
        ]
        bias -= step * bias_gradient
    return LinearProbe(tuple(weights), bias)


def _class_indices(labels: Sequence[bool]) -> tuple[list[int], list[int]]:
    positives = [index for index, label in enumerate(labels) if label]
    negatives = [index for index, label in enumerate(labels) if not label]
    if not positives or not negatives:
        raise ValueError("AUROC needs both correct and wrong examples.")
    return positives, negatives


def _check_finite(scores: Sequence[float]) -> None:
    # NaN never equals itself, so the tie loop in `auroc` would never advance.
    # Infinities sort, but a score that reaches one is a broken input too.
    if not all(math.isfinite(score) for score in scores):
        raise ValueError("Scores must be finite numbers.")


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Chance a correct example outscores a wrong one; ties count as half."""
    if len(scores) != len(labels):
        raise ValueError("Scores and labels must have the same length.")
    _check_finite(scores)
    positives, negatives = _class_indices(labels)

    # Rank-based, because the bootstrap calls this thousands of times.
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    positive_rank_sum = 0.0
    start = 0
    while start < len(order):
        end = start
        while end < len(order) and scores[order[end]] == scores[order[start]]:
            end += 1
        tied_rank = (start + 1 + end) / 2
        positive_rank_sum += tied_rank * sum(labels[i] for i in order[start:end])
        start = end

    wins = positive_rank_sum - len(positives) * (len(positives) + 1) / 2
    return wins / (len(positives) * len(negatives))


def auroc_difference_interval(
    first: Sequence[float],
    second: Sequence[float],
    labels: Sequence[bool],
) -> tuple[float, float]:
    """95% paired bootstrap interval on auroc(first) - auroc(second).

    Both scores use the same resampled examples. Each class is resampled on its
    own, so every resample keeps both classes and its original class sizes.
    """
    if not len(first) == len(second) == len(labels):
        raise ValueError("Scores and labels must have the same length.")
    # Checked once here, so a bad score fails before the 2,000 resamples.
    _check_finite(first)
    _check_finite(second)
    positives, negatives = _class_indices(labels)
    resampled_labels = [True] * len(positives) + [False] * len(negatives)

    rng = random.Random(BOOTSTRAP_SEED)
    differences = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        indices = rng.choices(positives, k=len(positives)) + rng.choices(
            negatives, k=len(negatives)
        )
        differences.append(
            auroc([first[index] for index in indices], resampled_labels)
            - auroc([second[index] for index in indices], resampled_labels)
        )
    differences.sort()
    tail = round(BOOTSTRAP_RESAMPLES * BOOTSTRAP_TAIL)
    return differences[tail], differences[-tail - 1]


# Characters that change a string without changing how it reads. After NFKC:
# combining grapheme joiner, variation selectors, Mongolian free variation
# selectors, Hangul fillers and the blank braille pattern.
_INVISIBLE = frozenset(
    {0x034F, 0x115F, 0x1160, 0x180B, 0x180C, 0x180D, 0x180F, 0x2800}
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)


def _is_invisible(char: str) -> bool:
    category = unicodedata.category(char)
    if category == "Cf" or ord(char) in _INVISIBLE:
        return True
    # Control characters, except tabs and newlines, which separate words.
    return category == "Cc" and not char.isspace()


def normalize_prompt(prompt: str) -> str:
    """The form `run_gate` uses to compare prompts.

    NFKC merges composed and decomposed accents and fullwidth letters.
    Invisible characters are dropped: format characters (category Cf, such
    as zero-width spaces and byte order marks), control characters other
    than whitespace, and the characters in `_INVISIBLE`. Then case and
    whitespace are ignored. This list is not complete, and look-alike
    letters from other scripts and paraphrases still count as different.
    """
    text = unicodedata.normalize("NFKC", prompt)
    text = "".join(char for char in text if not _is_invisible(char))
    text = unicodedata.normalize("NFKC", text.casefold())
    return " ".join(text.split())


def _check_no_repeats(examples: Sequence[ProbeExample], name: str) -> set[str]:
    counts = Counter(normalize_prompt(example.prompt) for example in examples)
    repeated = sorted(prompt for prompt, count in counts.items() if count > 1)
    if repeated:
        raise ValueError(
            f"The {name} set repeats {len(repeated)} prompt(s), for example "
            f"{repeated[0]!r}. Copies would count as independent examples."
        )
    return set(counts)


def run_gate(
    train: Sequence[ProbeExample],
    dev: Sequence[ProbeExample],
    extractor: ActivationExtractor,
    run_info: GateRunInfo,
) -> GateReport:
    """Train on `train`, then compare both scores on `dev`.

    The probe passes only if its activations are real, the 95% interval on its
    AUROC lead over token confidence stays above zero, and the 95% interval
    on its own AUROC stays above chance (see `gate_passes`).

    Prompts are compared after `normalize_prompt`. A prompt in both sets is
    an error, because it would inflate the probe's score. A prompt repeated
    within either set is an error, because the bootstrap and the class-size
    check would treat the copies as independent. So is a dev set with too
    few examples of either class, because the interval is then overconfident.
    """
    train_prompts = _check_no_repeats(train, "train")
    dev_prompts = _check_no_repeats(dev, "dev")
    if train_prompts & dev_prompts:
        raise ValueError("Train and dev sets must not share prompts.")
    labels = [example.correct for example in dev]
    if min(sum(labels), len(labels) - sum(labels)) < MIN_DEV_CLASS_SIZE:
        raise ValueError(
            f"Dev set needs at least {MIN_DEV_CLASS_SIZE} correct and "
            f"{MIN_DEV_CLASS_SIZE} wrong examples."
        )
    probe = train_probe(
        [extractor.extract(example.prompt, example.reply) for example in train],
        [example.correct for example in train],
    )
    # Logits, not probabilities: AUROC only uses the order, and logits keep
    # it where the sigmoid would round to 1.0.
    probe_scores = [
        probe.logit(extractor.extract(example.prompt, example.reply)) for example in dev
    ]
    token_scores = [example.token_confidence for example in dev]
    difference_low, difference_high = auroc_difference_interval(
        probe_scores, token_scores, labels
    )
    # Every constant score has AUROC 0.5, so the same paired bootstrap against
    # one gives the interval on AUROC(probe) - 0.5.
    chance_low, chance_high = auroc_difference_interval(
        probe_scores, [CHANCE_AUROC] * len(dev), labels
    )
    probe_auroc_low = CHANCE_AUROC + chance_low
    return GateReport(
        probe_auroc=auroc(probe_scores, labels),
        probe_auroc_low=probe_auroc_low,
        probe_auroc_high=CHANCE_AUROC + chance_high,
        token_auroc=auroc(token_scores, labels),
        difference_low=difference_low,
        difference_high=difference_high,
        train_examples=len(train),
        dev_examples=len(dev),
        passed=gate_passes(extractor.mocked, difference_low, probe_auroc_low),
        mocked=extractor.mocked,
        run_info=run_info,
    )
