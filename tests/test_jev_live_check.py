"""The B6 live-check script, against a fake TypeSafe server. No network."""

import asyncio
import csv
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app_assignment_1.config import Settings
from app_assignment_1.jev import create_jev_http_client
from app_assignment_1.labels import load_labels

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "jev_live_check.py"
LABELS = load_labels(Path("config/labels.json"))
OPTIONS = [item.label for item in LABELS.items]
SECRET = "tsk-live-check-secret"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("jev_live_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = load_script()
INTENTS = live.banking77_intents(LABELS)


def train_rows(per_intent: int = 6) -> list[tuple[str, str]]:
    """Short, medium and long messages for every intent."""
    rows = []
    for intent in INTENTS:
        for i in range(per_intent):
            words = [3, 15, 25][i % 3]
            rows.append((" ".join([f"{intent}-{i}"] * words), intent))
    return rows


# Sampling


def test_sample_covers_every_intent_and_keeps_no_text() -> None:
    rows = train_rows()
    items = live.draw_sample(rows, INTENTS, 300, seed=1)
    assert len(items) == 300
    per_intent = {intent: 0 for intent in INTENTS}
    for item in items:
        per_intent[item["intent"]] += 1
        assert set(item) == {"row", "intent", "words"}
        assert rows[item["row"]][1] == item["intent"]
    # 300 over 77 intents: 69 intents get 4 and 8 get 3.
    assert sorted(set(per_intent.values())) == [3, 4]
    assert list(per_intent.values()).count(4) == 69


def test_sample_takes_each_length_band_in_turn() -> None:
    items = live.draw_sample(train_rows(), INTENTS, 231, seed=1)
    bands: dict[str, set[str]] = {}
    for item in items:
        bands.setdefault(item["intent"], set()).add(live.word_band(item["words"]))
    # Three per intent: one from each band.
    assert all(found == set(live.BANDS) for found in bands.values())


def test_sample_is_repeatable_by_seed() -> None:
    rows = train_rows()
    assert live.draw_sample(rows, INTENTS, 300, 7) == live.draw_sample(
        rows, INTENTS, 300, 7
    )
    assert live.draw_sample(rows, INTENTS, 300, 7) != live.draw_sample(
        rows, INTENTS, 300, 8
    )


def test_sample_refuses_too_few_messages_or_wrong_intents() -> None:
    with pytest.raises(ValueError, match="Not enough messages"):
        live.draw_sample(train_rows(per_intent=2), INTENTS, 300, 1)
    with pytest.raises(ValueError, match="77 Banking77 intents"):
        live.draw_sample(train_rows()[:-6], INTENTS, 300, 1)


def test_word_bands() -> None:
    assert [live.word_band(n) for n in (1, 10, 11, 20, 21)] == [
        "<=10",
        "<=10",
        "11-20",
        "11-20",
        ">20",
    ]


def write_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["text", "category"])
        writer.writerows(rows)


def test_sample_command_writes_ids_not_text(tmp_path: Path) -> None:
    train = tmp_path / "train.csv"
    write_csv(train, train_rows())
    out = tmp_path / "sample.json"
    assert live.main(["sample", "--train", str(train), "--out", str(out)]) == 0
    sample = json.loads(out.read_text())
    assert sample["sha256"] == live.file_digest(train)
    # train_rows() has equal numbers of short, medium and long messages.
    assert sample["band_shares"] == pytest.approx(
        {"<=10": 1 / 3, "11-20": 1 / 3, ">20": 1 / 3}
    )
    assert len(sample["items"]) == 300
    assert "card_arrival-0" not in out.read_text()


def test_run_refuses_a_changed_training_file(tmp_path: Path) -> None:
    train = tmp_path / "train.csv"
    write_csv(train, train_rows())
    sample = tmp_path / "sample.json"
    live.main(["sample", "--train", str(train), "--out", str(sample)])
    write_csv(train, train_rows(per_intent=7))
    with pytest.raises(ValueError, match="differs"):
        live._load_sample(sample, train)


def test_read_train_needs_text_and_category(tmp_path: Path) -> None:
    path = tmp_path / "train.csv"
    path.write_text("text,label\nhello,0\n")
    with pytest.raises(ValueError, match="'text' and 'category'"):
        live.read_train(path)


# Calls


def jev_payload(intent_total: float = 1.0) -> dict[str, Any]:
    rest = (intent_total - 0.9) / 77
    probabilities = {label: rest for label in OPTIONS}
    probabilities["card_arrival"] = 0.9
    return {
        "model": "jev-1.14.0",
        "answers": {
            "route": {
                "type": "choice",
                "choice": "complex",
                "confidence": 0.4,
                "probabilities": {"standard": 0.3, "complex": 0.7},
            },
            "intent": {
                "type": "choice",
                "choice": "card_arrival",
                "confidence": 0.9,
                "probabilities": probabilities,
            },
        },
        "usage": {"input_tokens": 2900, "output_tokens": 30},
    }


class FakeJev:
    def __init__(self, statuses: list[int | str] | None = None) -> None:
        self.statuses = statuses or []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        status = self.statuses.pop(0) if self.statuses else 200
        if status == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(
            int(status),
            json=jev_payload() if status == 200 else {"error": "x"},
            headers={"x-typesafe-request-id": f"req_{len(self.bodies)}"},
        )


class Clock:
    """Each reading is `step` seconds after the last, so a call takes `step`."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def call(
    server: FakeJev, n: int = 3, step: float = 0.5
) -> tuple[list[dict[str, Any]], int]:
    client = create_jev_http_client(
        Settings(_env_file=None, jev_api_key=SECRET), httpx.MockTransport(server)
    )
    messages = [
        ({"row": i, "intent": "card_arrival", "words": 4}, f"my card {i} is late")
        for i in range(n)
    ]
    records: list[dict[str, Any]] = []

    async def go() -> int:
        async with client:
            return await live.run_calls(
                client, "jev-1.14.0", LABELS, messages, records.append, Clock(step)
            )

    return records, asyncio.run(go())


def test_run_sends_the_gateway_request_and_logs_each_call() -> None:
    server = FakeJev()
    records, made = call(server)
    assert made == 3
    body = server.bodies[0]
    assert body["model"] == "jev-1.14.0"
    assert list(body["questions"]["intent"]["criteria"]) == OPTIONS
    record = records[0]
    assert record["status"] == 200
    assert record["latency_ms"] == pytest.approx(500)
    assert record["model"] == "jev-1.14.0"
    assert record["request_id"] == "req_1"
    assert "x-typesafe-request-id" in record["header_names"]
    assert record["error_types"] == {"route": None, "intent": None}
    assert record["usage"] == {"input_tokens": 2900, "output_tokens": 30}


def test_the_log_holds_no_message_text_or_key() -> None:
    records, _ = call(FakeJev())
    dumped = json.dumps(records)
    assert "my card" not in dumped
    assert SECRET not in dumped


def test_timeouts_and_errors_are_logged_with_their_latency() -> None:
    records, _ = call(FakeJev(["timeout", 529, 200]))
    assert [r["status"] for r in records] == ["timeout", 529, 200]
    assert all(r["latency_ms"] == pytest.approx(500) for r in records)
    assert "model" not in records[1]


def test_run_stops_on_a_rejected_key() -> None:
    records, made = call(FakeJev([401]), n=5)
    assert made == 1
    assert [r["status"] for r in records] == [401]


def test_run_stops_after_five_failed_calls_in_a_row() -> None:
    records, made = call(FakeJev([529, 529, 200] + [529] * 5), n=20)
    assert made == 8
    assert [r["status"] for r in records][-5:] == [529] * 5


def test_run_refuses_to_overwrite_a_log(tmp_path: Path) -> None:
    train = tmp_path / "train.csv"
    write_csv(train, train_rows())
    sample = tmp_path / "sample.json"
    live.main(["sample", "--train", str(train), "--out", str(sample)])
    out = tmp_path / "calls.jsonl"
    out.write_text("kept\n")
    args = ["run", "--train", str(train), "--sample", str(sample), "--out", str(out)]
    assert live.main(args) == 2
    assert out.read_text() == "kept\n"


# Summary


def record(
    latency_ms: float,
    words: int = 5,
    status: int | str = 200,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": status,
        "latency_ms": latency_ms,
        "words": words,
        "band": live.word_band(words),
    }
    if status == 200:
        payload = payload or jev_payload()
        values |= {
            "header_names": ["content-type", "x-typesafe-request-id"],
            "request_id": "req_1",
            "model": payload["model"],
            "usage": payload["usage"],
            "answers": payload["answers"],
            "error_types": {"route": None, "intent": None},
        }
    return values


def test_summary_reports_the_timeout_share_by_length() -> None:
    records = [record(500) for _ in range(18)] + [
        record(2500, words=25),
        record(15000, words=25, status="timeout"),
    ]
    text = live.summarize(records)
    assert "**Over the gateway's 2 s timeout, in this sample:** 10.0% (2 of 20;" in text
    assert "length mix" not in text
    assert "| >20 | 2 | 100.0% (2 of 2;" in text
    assert "| <=10 | 18 | 0.0% (0 of 18;" in text
    assert "`jev-1.14.0` in 19 of 19" in text
    assert "**Status:** 200: 19, timeout: 1" in text


def test_summary_reports_rounding_against_both_limits() -> None:
    # Two decimals, total 0.9: over 0.05 but inside 0.39 for 78 options.
    payload = jev_payload()
    rounded = {label: 0.0 for label in OPTIONS}
    rounded["card_arrival"] = 0.9
    payload["answers"]["intent"]["probabilities"] = rounded
    text = live.summarize([record(400, payload=payload)])
    assert "| intent | 1 | 1 | 0.100 | 0.100 | 1 | 0 |" in text
    assert "| route | 1 | 1 | 0.000 | 0.000 | 0 | 0 |" in text


def test_summary_checks_the_confidence_formula_where_it_can() -> None:
    # Route: top 0.7 of 2 options gives (0.7 - 0.5) / 0.5 = 0.4, as sent.
    # Intent: the payload has full-precision probabilities, so even the 0.0013
    # gap between the formula (0.8987) and the top probability (0.9) is clear,
    # and a confidence of 0.9 matches the top probability.
    text = live.summarize([record(400)])
    assert "| route | 1 | 1 | 0.000 | 0.000 | 0 | 0 | 1 of 1 | 0 of 1 |" in text
    intent = next(line for line in text.splitlines() if line.startswith("| intent"))
    assert intent.endswith("| 0 of 1 | 1 of 1 |")


def two_decimal_intent(top: float, confidence: float) -> dict[str, Any]:
    payload = jev_payload()
    probabilities = {label: 0.0 for label in OPTIONS}
    probabilities["card_arrival"] = top
    probabilities["unknown"] = round(1 - top, 2)
    payload["answers"]["intent"] |= {
        "probabilities": probabilities,
        "confidence": confidence,
    }
    return payload


def test_two_decimal_intent_answers_cannot_tell_the_two_apart() -> None:
    # Rounded to 0.01, the formula and the top probability are within both
    # tolerances of each other for 78 options, so no answer counts.
    text = live.summarize([record(400, payload=two_decimal_intent(0.87, 0.87))])
    intent = next(line for line in text.splitlines() if line.startswith("| intent"))
    assert intent.endswith("| 0 of 0 | 0 of 0 |")
    assert "intent ±0.010 for the formula and ±0.010 for the top probability" in text


def test_rounded_route_answers_that_follow_the_formula_all_match() -> None:
    # Sebastian's case on #79: p from 0.5 to 0.97, confidence = 2p - 1, both
    # then rounded to two decimals. A fixed 0.01 match counted 212 of 300.
    records = []
    for i in range(300):
        p = 0.5 + 0.47 * i / 299
        payload = jev_payload()
        payload["answers"]["route"] |= {
            "choice": "complex",
            "confidence": round(2 * p - 1, 2),
            "probabilities": {"standard": round(1 - p, 2), "complex": round(p, 2)},
        }
        records.append(record(400, payload=payload))
    text = live.summarize(records)
    route = next(line for line in text.splitlines() if line.startswith("| route"))
    formula, top = route.rstrip(" |").split(" | ")[-2:]
    matched, informative = (int(x) for x in formula.split(" of "))
    assert matched == informative >= 290
    assert top == f"0 of {informative}"
    assert "route ±0.015 for the formula and ±0.010 for the top probability" in text


def test_confidence_tolerances() -> None:
    near_formula, near_top = live.confidence_tolerances(2, 0.01)
    assert near_formula == pytest.approx(0.015)
    assert near_top == pytest.approx(0.01)
    near_formula, _ = live.confidence_tolerances(78, 0.01)
    assert near_formula == pytest.approx(78 / 77 * 0.005 + 0.005)


def test_fast_failures_are_left_out_of_the_timeout_share() -> None:
    # 18 answers, 2 over 2 s, and 10 fast 429s. Counting the 429s would show
    # 2 of 30 (6.7%) instead of 2 of 20 (10%).
    records = [record(500) for _ in range(18)] + [
        record(2500, words=25),
        record(15000, words=25, status="timeout"),
    ]
    records += [record(80, status=429) for _ in range(10)]
    text = live.summarize(records)
    assert "in this sample:** 10.0% (2 of 20;" in text
    assert "Left out: 10 failed calls that returned early (429: 10)." in text
    assert "## Latency (answered calls and timeouts, ms)" in text


def test_summary_sees_a_route_confidence_equal_to_the_top_probability() -> None:
    payload = jev_payload()
    payload["answers"]["route"]["confidence"] = 0.7
    text = live.summarize([record(400, payload=payload)])
    assert "| 0 of 1 | 1 of 1 |" in text


def test_summary_counts_request_ids_and_tokens() -> None:
    records = [record(400), record(400)]
    records[1]["request_id"] = None
    text = live.summarize(records, price_per_million=0.042)
    assert "50.0% (1 of 2;" in text
    assert "input median 2900, max 2900" in text
    assert "$0.0002" in text


SHARES = {"<=10": 0.59, "11-20": 0.31, ">20": 0.10}


def by_band() -> list[dict[str, Any]]:
    """10 calls per band; 0, 1 and 5 of them over 2 s."""
    records = []
    for words, slow in ((5, 0), (15, 1), (25, 5)):
        records += [record(2500 if i < slow else 500, words=words) for i in range(10)]
    return records


def test_summary_reweights_the_timeout_rate_to_the_training_mix() -> None:
    text = live.summarize(by_band(), shares=SHARES)
    # The sample: 6 of 30. The training mix: 0.31 x 10% + 0.10 x 50% = 8.1%.
    assert "in this sample:** 20.0% (6 of 30;" in text
    assert (
        "**In the training split's length mix** (<=10 59%, 11-20 31%, >20 10%):" in text
    )
    assert "8.1% (95% CI" in text


def test_the_reweighted_interval_contains_the_estimate() -> None:
    result = live.reweighted_share(by_band(), lambda r: r["latency_ms"] > 2000, SHARES)
    assert result is not None
    estimate, low, high = result
    assert estimate == pytest.approx(0.081)
    assert 0 <= low < estimate < high <= 1


def test_no_reweighted_rate_when_a_band_has_no_calls() -> None:
    records = [r for r in by_band() if r["band"] != ">20"]
    text = live.summarize(records, shares=SHARES)
    assert "not available: a band has no calls." in text


def test_band_shares() -> None:
    rows = [("a b", "x"), ("a " * 15, "x"), ("a " * 30, "x"), ("a", "x")]
    assert live.band_shares(rows) == {"<=10": 0.5, "11-20": 0.25, ">20": 0.25}


def test_wilson_interval() -> None:
    low, high = live.wilson(5, 100)
    assert low == pytest.approx(0.0215, abs=0.001)
    assert high == pytest.approx(0.1118, abs=0.001)
    assert live.wilson(0, 0) == (0.0, 1.0)


def test_decimals() -> None:
    assert [live.decimals(v) for v in (1.0, 0.5, 0.25, 0.013, 1e-05)] == [
        0,
        1,
        2,
        3,
        5,
    ]


def test_the_commands_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    server = FakeJev()
    monkeypatch.setattr(
        live,
        "create_jev_http_client",
        lambda settings: create_jev_http_client(
            settings.model_copy(update={"jev_api_key": SecretStr(SECRET)}),
            httpx.MockTransport(server),
        ),
    )
    train = tmp_path / "train.csv"
    write_csv(train, train_rows())
    sample = tmp_path / "sample.json"
    out = tmp_path / "calls.jsonl"
    assert live.main(["sample", "--train", str(train), "--out", str(sample)]) == 0
    run = ["run", "--train", str(train), "--sample", str(sample), "--out", str(out)]
    assert live.main([*run, "--limit", "2"]) == 0
    lines = out.read_text().splitlines()
    assert [json.loads(line)["call"] for line in lines] == [0, 1]
    # The text sent is the sampled row's text.
    rows = live.read_train(train)
    first = json.loads(sample.read_text())["items"][0]
    assert server.bodies[0]["state"] == rows[first["row"]][0]
    assert live.main(["summarize", str(out), "--sample", str(sample)]) == 0
    printed = capsys.readouterr().out
    assert "# Live Jev check: 2 calls" in printed
    assert "In the training split's length mix" in printed
