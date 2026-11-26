"""Confidence scores for the small model's intent, used to escalate to the
large model when the small one looks unsure.

Today the only scorer is token confidence: the joint probability of the
intent label's tokens, from the logprobs vLLM returns with the reply. The
linear probe (probe.py, docs/PROBE.md) is meant to replace it only if it
passes the gate; until then it stays offline.
"""

import json
import math
import re
from collections.abc import Sequence
from typing import Any

from app_assignment_1.schemas import BackendReply

# The opening of the intent value in the reply JSON. `intent` is the first
# field of BackendReply, so its tokens come before the reply text.
_INTENT_VALUE = re.compile(r'"intent"\s*:\s*"')


class ScoredReply(BackendReply):
    """A backend reply with a confidence score for its intent. Not part of the
    JSON schema sent to the model; the backend fills it from logprobs."""

    label_confidence: float | None = None


def label_confidence(content: str, tokens: Sequence[Any]) -> float | None:
    """Joint probability of the tokens that spell the intent value.

    `tokens` is vLLM's `choices[0].logprobs.content`: one object per output
    token, with `token` and `logprob`. A token counts when any of its
    characters fall inside the intent value, so a token that also holds the
    closing quote still counts. Returns None when the tokens do not spell
    `content` exactly, a log probability is above 0, or the value cannot be
    found, rather than guess.
    """
    try:
        texts = [str(item["token"]) for item in tokens]
        logprobs = [float(item["logprob"]) for item in tokens]
    except (KeyError, TypeError, ValueError):
        return None
    # A log probability is never above 0. A larger value is a malformed block,
    # and exp() of it can overflow; better no score than a wrong one.
    if any(logprob > 0 for logprob in logprobs):
        return None
    if "".join(texts) != content:
        return None
    match = _INTENT_VALUE.search(content)
    if match is None:
        return None
    start = match.end()
    try:
        # Find where the JSON string ends, escapes included; start - 1 is the
        # opening quote.
        _, end = json.JSONDecoder().raw_decode(content, start - 1)
    except ValueError:
        return None
    # `end` is just past the closing quote; the value itself ends one before.
    end -= 1
    if end <= start:
        return None
    total = 0.0
    offset = 0
    counted = False
    for text, logprob in zip(texts, logprobs, strict=True):
        token_start, offset = offset, offset + len(text)
        if offset > start and token_start < end:
            if math.isnan(logprob):
                return None
            total += logprob
            counted = True
    if not counted:
        return None
    try:
        return min(1.0, max(0.0, math.exp(total)))
    except OverflowError:
        return None
