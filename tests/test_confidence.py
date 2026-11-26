"""Token confidence for the intent label (confidence.py)."""

import json
import math

import pytest

from app_assignment_1.confidence import label_confidence

CONTENT = json.dumps({"intent": "card_arrival", "reply": "Soon.", "policy_facts": []})


def split(content: str, cuts: list[int]) -> list[str]:
    bounds = [0, *cuts, len(content)]
    return [content[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


def tokens(pieces: list[str], logprobs: list[float]) -> list[dict[str, object]]:
    return [{"token": p, "logprob": lp} for p, lp in zip(pieces, logprobs, strict=True)]


def test_joint_probability_of_the_label_tokens() -> None:
    start = CONTENT.index("card_arrival")
    pieces = split(CONTENT, [start, start + 4, start + 12])
    score = label_confidence(CONTENT, tokens(pieces, [-9.0, -0.5, -0.25, -9.0]))
    assert score == pytest.approx(math.exp(-0.75))


def test_a_token_that_holds_the_closing_quote_still_counts() -> None:
    start = CONTENT.index("card_arrival")
    # "card", then '_arrival", "' runs past the value into the next key.
    pieces = split(CONTENT, [start, start + 4, start + 16])
    score = label_confidence(CONTENT, tokens(pieces, [-9.0, -0.5, -0.25, -9.0]))
    assert score == pytest.approx(math.exp(-0.75))


def test_tokens_that_do_not_spell_the_content_give_no_score() -> None:
    assert label_confidence(CONTENT, tokens(["{}"], [-0.1])) is None


@pytest.mark.parametrize(
    "bad",
    [
        [{"token": "x"}],
        [{"logprob": -1.0}],
        ["not a dict"],
        [{"token": CONTENT, "logprob": "nope"}],
    ],
)
def test_malformed_tokens_give_no_score(bad: list[object]) -> None:
    assert label_confidence(CONTENT, bad) is None


def test_no_intent_value_gives_no_score() -> None:
    content = json.dumps({"reply": "Soon."})
    assert label_confidence(content, tokens([content], [-0.1])) is None


def test_nan_logprob_gives_no_score() -> None:
    assert label_confidence(CONTENT, tokens([CONTENT], [math.nan])) is None


def test_minus_infinity_is_probability_zero() -> None:
    assert label_confidence(CONTENT, tokens([CONTENT], [-math.inf])) == 0.0


def test_positive_logprob_gives_no_score() -> None:
    content = '{"intent":"card_arrival"}'
    start = content.index("card_arrival")
    tokens = [
        {"token": content[:start], "logprob": -0.1},
        {"token": "card_arrival", "logprob": 1000},
        {"token": content[start + 12 :], "logprob": -0.1},
    ]
    assert label_confidence(content, tokens) is None
