"""Offline linear probe skeleton: do activations predict a correct reply?

The probe scores each reply from Qwen3-1.7B activations. The gate in
docs/TEAM_PLAN.md compares that score with the model's own token-probability
confidence by AUROC on the dev set. Nothing here runs in the gateway.

Only `MockActivationExtractor` exists so far, so results from it say nothing
about the real model. See docs/PROBE.md for what is still to build.
"""

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field

from app_assignment_1.schemas import StrictModel


class ProbeExample(StrictModel):
    """One graded reply. `correct` is the label the probe learns to predict."""

    prompt: str = Field(min_length=1)
    reply: str
    correct: bool
    token_confidence: float = Field(ge=0, le=1)


class GateReport(StrictModel):
    probe_auroc: float = Field(ge=0, le=1)
    token_auroc: float = Field(ge=0, le=1)
    train_examples: int = Field(ge=1)
    dev_examples: int = Field(ge=2)
    passed: bool
    mocked: bool


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
        """Estimated probability that the reply is correct."""
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


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Chance a correct reply outscores a wrong one; ties count as half."""
    if len(scores) != len(labels):
        raise ValueError("Scores and labels must have the same length.")
    positives = [s for s, label in zip(scores, labels, strict=True) if label]
    negatives = [s for s, label in zip(scores, labels, strict=True) if not label]
    if not positives or not negatives:
        raise ValueError("AUROC needs both correct and wrong replies.")
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positives
        for negative in negatives
    )
    return wins / (len(positives) * len(negatives))


def run_gate(
    train: Sequence[ProbeExample],
    dev: Sequence[ProbeExample],
    extractor: ActivationExtractor,
) -> GateReport:
    """Train on `train`, then compare both scores on `dev`.

    The probe passes only if its AUROC is strictly higher and its activations
    are real. A prompt in both sets is an error, because it would inflate the
    probe's score. Paraphrases are not detected.
    """
    if {example.prompt for example in train} & {example.prompt for example in dev}:
        raise ValueError("Train and dev sets must not share prompts.")
    probe = train_probe(
        [extractor.extract(example.prompt, example.reply) for example in train],
        [example.correct for example in train],
    )
    labels = [example.correct for example in dev]
    probe_auroc = auroc(
        [
            probe.score(extractor.extract(example.prompt, example.reply))
            for example in dev
        ],
        labels,
    )
    token_auroc = auroc([example.token_confidence for example in dev], labels)
    return GateReport(
        probe_auroc=probe_auroc,
        token_auroc=token_auroc,
        train_examples=len(train),
        dev_examples=len(dev),
        passed=not extractor.mocked and probe_auroc > token_auroc,
        mocked=extractor.mocked,
    )
