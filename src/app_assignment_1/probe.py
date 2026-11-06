"""Offline linear probe skeleton: do activations predict a correct intent?

The probe scores each intent prediction from Qwen3-1.7B activations. The gate in
docs/TEAM_PLAN.md compares that score with the model's own token-probability
confidence by AUROC on the dev set. Nothing here runs in the gateway.

Only `MockActivationExtractor` exists so far, so results from it say nothing
about the real model. See docs/PROBE.md for what is still to build.
"""

import hashlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field

from app_assignment_1.schemas import StrictModel

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
BOOTSTRAP_TAIL = 0.025  # 2.5% in each tail gives a 95% interval.

# Below this, the interval is overconfident. See docs/PROBE.md.
MIN_DEV_CLASS_SIZE = 20


class ProbeExample(StrictModel):
    """One model output. `correct` is the label the probe learns to predict.

    `token_confidence` is a probability, so a mean log probability must be
    exponentiated first. That does not change the order of scores, or AUROC.
    """

    prompt: str = Field(min_length=1)
    reply: str
    correct: bool
    token_confidence: float = Field(ge=0, le=1)


class GateRunInfo(StrictModel):
    """What a gate run measured, so a saved report can be cited and repeated."""

    model: str = Field(min_length=1)
    layer: int = Field(ge=0)
    pooling: str = Field(min_length=1)
    token_confidence_method: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)


class GateReport(StrictModel):
    probe_auroc: float = Field(ge=0, le=1)
    token_auroc: float = Field(ge=0, le=1)
    difference_low: float = Field(ge=-1, le=1)
    difference_high: float = Field(ge=-1, le=1)
    train_examples: int = Field(ge=2)
    dev_examples: int = Field(ge=2)
    passed: bool
    mocked: bool
    run_info: GateRunInfo


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

    def score(self, features: Sequence[float]) -> float:
        """Estimated probability that the predicted intent is correct."""
        logit = self.bias + sum(
            weight * value for weight, value in zip(self.weights, features, strict=True)
        )
        if not math.isfinite(logit):
            raise ValueError("Probe features must be finite numbers.")
        return _sigmoid(logit)


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


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Chance a correct example outscores a wrong one; ties count as half."""
    if len(scores) != len(labels):
        raise ValueError("Scores and labels must have the same length.")
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


def _normalized_prompt(prompt: str) -> str:
    return " ".join(prompt.casefold().split())


def run_gate(
    train: Sequence[ProbeExample],
    dev: Sequence[ProbeExample],
    extractor: ActivationExtractor,
    run_info: GateRunInfo,
) -> GateReport:
    """Train on `train`, then compare both scores on `dev`.

    The probe passes only if the 95% interval on its AUROC lead stays above
    zero and its activations are real. A prompt in both sets is an error,
    ignoring case and whitespace, because it would inflate the probe's score.
    Paraphrases are not detected. So is a dev set with too few examples of
    either class, because the interval is then overconfident.
    """
    train_prompts = {_normalized_prompt(example.prompt) for example in train}
    dev_prompts = {_normalized_prompt(example.prompt) for example in dev}
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
    probe_scores = [
        probe.score(extractor.extract(example.prompt, example.reply)) for example in dev
    ]
    token_scores = [example.token_confidence for example in dev]
    difference_low, difference_high = auroc_difference_interval(
        probe_scores, token_scores, labels
    )
    return GateReport(
        probe_auroc=auroc(probe_scores, labels),
        token_auroc=auroc(token_scores, labels),
        difference_low=difference_low,
        difference_high=difference_high,
        train_examples=len(train),
        dev_examples=len(dev),
        passed=not extractor.mocked and difference_low > 0,
        mocked=extractor.mocked,
        run_info=run_info,
    )
