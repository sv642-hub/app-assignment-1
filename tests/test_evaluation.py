"""Offline evaluation: datasets, scoring, the oracle and the runner script."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from app_assignment_1.config import Settings
from app_assignment_1.evaluation import (
    Message,
    confusions,
    gold_classes,
    oracle,
    parse_banking77,
    parse_labelled_csv,
    reply_format_errors,
    score,
    sha256,
    stratified_sample,
    wilson,
)
from app_assignment_1.labels import load_labels
from app_assignment_1.router import MockJevRouter
from app_assignment_1.schemas import BackendConfig, BackendReply, PolicyCard
from app_assignment_1.service import Gateway
from app_assignment_1.vllm import BackendOutputError, BackendTimeoutError

ROOT = Path(__file__).resolve().parents[1]
LABELS = load_labels(ROOT / "config/labels.json")
CARDS = TypeAdapter(dict[str, PolicyCard]).validate_json(
    (ROOT / "config/policy_cards.json").read_text()
)
ARRIVAL = CARDS["card_arrival"].facts
LOST = CARDS["lost_or_stolen_card"].facts


def item(row, gold, predicted, **extra):
    return {"row": row, "gold": gold, "predicted": predicted, "error": None,
            "latency_ms": 100.0, "model": "qwen3-1.7b", **extra}  # fmt: skip


def test_banking77_names_map_to_our_labels():
    body = (
        b"text,category\n"
        b"a,card_arrival\n"
        b"b,Refund_not_showing_up\n"
        b"c,reverted_card_payment?\n"
    )
    messages = parse_banking77(body, LABELS)
    assert [(m.row, m.gold) for m in messages] == [
        (0, "card_arrival"),
        (1, "refund_not_showing_up"),
        (2, "reverted_card_payment"),
    ]
    with pytest.raises(ValueError, match="not a Banking77 intent"):
        parse_banking77(b"text,category\na,made_up\n", LABELS)


def test_labelled_csv_accepts_labels_banking77_names_and_out_of_scope():
    body = (
        b"text,intent,writer\n"
        b"a,card_arrival,x\n"
        b"b,Refund_not_showing_up,x\n"
        b"c,out_of_scope,x\n"
        b"d,unknown,x\n"
    )
    golds = [m.gold for m in parse_labelled_csv(body, LABELS)]
    assert golds == ["card_arrival", "refund_not_showing_up", "unknown", "unknown"]
    with pytest.raises(ValueError, match="not a label"):
        parse_labelled_csv(b"text,intent\na,made_up\n", LABELS)


def test_labelled_csv_reads_excel_bom_and_names_bad_rows():
    bom = "\ufefftext,intent\na,card_arrival\n".encode()
    assert [m.gold for m in parse_labelled_csv(bom, LABELS)] == ["card_arrival"]
    with pytest.raises(ValueError, match="no intent column"):
        parse_labelled_csv(b"text,label\na,card_arrival\n", LABELS)
    with pytest.raises(ValueError, match="Row 1 is missing a field"):
        parse_labelled_csv(b"text,intent\na,card_arrival\nb\n", LABELS)


def test_macro_f1_averages_over_the_intents_in_the_run():
    classes = gold_classes(LABELS)
    assert len(classes) == 77 and "unknown" not in classes
    items = [
        item(0, "card_arrival", "card_arrival"),
        item(1, "card_arrival", "unknown"),  # unknown is wrong, not a free pass
        item(2, "age_limit", "card_arrival"),  # a false positive for card_arrival
        {**item(3, "age_limit", None), "error": "BackendTimeoutError"},
    ]
    result = score(items, LABELS)
    assert result["accuracy"] == 0.25
    assert result["errors"] == {"BackendTimeoutError": 1}
    assert result["predicted_unknown"] == 1
    # card_arrival: precision 1/2, recall 1/2, F1 0.5; age_limit: F1 0. The
    # other 75 intents have no messages and no predictions, so they are left
    # out, and `unknown` is never a class.
    assert result["macro_f1_classes"] == 2
    assert result["macro_f1"] == pytest.approx(0.25)
    assert result["per_class_recall"] == {"card_arrival": 0.5, "age_limit": 0.0}
    assert result["latency_ms_p50"] == 100.0


def test_wilson_interval():
    low, high = wilson(50, 100)
    assert low == pytest.approx(0.404, abs=0.001)
    assert high == pytest.approx(0.596, abs=0.001)
    assert wilson(0, 0) == (0.0, 0.0)


def test_oracle_pays_for_the_large_model_only_where_it_helps():
    small = [
        item(0, "card_arrival", "card_arrival"),  # small right
        item(1, "age_limit", "unknown"),  # small wrong, large right
        item(2, "atm_support", "unknown"),  # both wrong
    ]
    large = [
        item(0, "card_arrival", "card_arrival", model="qwen3-4b"),
        item(1, "age_limit", "age_limit", model="qwen3-4b"),
        item(2, "atm_support", "age_limit", model="qwen3-4b"),
    ]
    combined = oracle(small, large)
    assert [c["model"] for c in combined] == ["qwen3-1.7b", "qwen3-4b", "qwen3-1.7b"]
    assert score(combined, LABELS)["correct"] == 2
    with pytest.raises(ValueError, match="only in the large"):
        oracle(small[:2], large)


class FixedBackend:
    def __init__(self, name, route, reply):
        self.config = BackendConfig(name=name, route=route, latency_ms=0)
        self.reply = reply

    async def generate(self, message):
        return self.reply


@pytest.mark.parametrize(
    "reply",
    [
        BackendReply(intent="card_arrival", reply="It arrives soon.", policy_facts=[]),
        BackendReply(intent="card_arrival", reply="", policy_facts=[]),
        BackendReply(intent="card_arrival", reply="Hi {name}", policy_facts=[]),
        BackendReply(intent="made_up", reply="Hi.", policy_facts=[]),
        BackendReply(intent="unknown", reply="Please describe it.", policy_facts=[]),
        BackendReply(intent="unknown", reply="Please.", policy_facts=["a fact"]),
        BackendReply(intent="age_limit", reply="", policy_facts=[]),
        BackendReply(intent="age_limit", reply="", policy_facts=["a fact"]),
        # #109 run: uncarded intent with leftover facts passes (#127).
        BackendReply(intent="age_limit", reply="You must be 18.", policy_facts=["x"]),
        # #109 run: unknown with no text and no facts passes (#128).
        BackendReply(intent="unknown", reply="", policy_facts=[]),
        BackendReply(intent="unknown", reply="   ", policy_facts=[]),
        BackendReply(intent="unknown", reply="", policy_facts=["a fact"]),
        # Facts checked against the chosen card (#126).
        BackendReply(intent="card_arrival", reply="Soon.", policy_facts=[ARRIVAL[0]]),
        BackendReply(intent="card_arrival", reply="Soon.", policy_facts=ARRIVAL[::-1]),
        BackendReply(
            intent="card_arrival", reply="Soon.", policy_facts=[ARRIVAL[0], "made up"]
        ),
        BackendReply(intent="card_arrival", reply="Soon.", policy_facts=[LOST[0]]),
        BackendReply(intent="card_arrival", reply="", policy_facts=["made up"]),
    ],
)
def test_format_errors_agree_with_the_gateway(reply):
    settings = Settings(_env_file=None)
    backends = [
        FixedBackend("qwen3-1.7b", "standard", reply),
        FixedBackend("qwen3-4b", "complex", reply),
    ]
    gateway = Gateway(settings, MockJevRouter(settings.intents_path), backends)
    trace = asyncio.run(gateway.chat("card arrival")).trace
    assert reply_format_errors(reply, LABELS, CARDS) == trace.format_errors


def load_script():
    spec = importlib.util.spec_from_file_location(
        "run_eval", ROOT / "scripts/run_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_on_a_csv_with_the_mocks(tmp_path, capsys):
    run_eval = load_script()
    texts = ["When will my new card arrive?", "How old do I need to be?"]
    data = tmp_path / "fresh.csv"
    data.write_text(f"text,intent\n{texts[0]},card_arrival\n{texts[1]},age_limit\n")
    small, large = tmp_path / "small.jsonl", tmp_path / "large.jsonl"
    for target, out in (("small", small), ("large", large)):
        args = ["run", "--target", target, "--mock", "--dataset-csv", str(data)]
        assert run_eval.main([*args, "--out", str(out)]) == 0

    header, items = run_eval.read_results(small)
    assert header["dataset"] == "fresh" and header["mocked"] is True
    assert header["dataset_size"] == 2 and len(items) == 2
    assert items[0]["gold"] == "card_arrival" and items[0]["error"] is None
    # No message text anywhere in the results.
    assert not any(text in small.read_text() for text in texts)

    # Running again skips the rows already done and adds nothing.
    assert run_eval.main([*args, "--out", str(large)]) == 0
    assert len(large.read_text().splitlines()) == 3

    # A results file from another run is refused, not mixed.
    with pytest.raises(SystemExit, match="different run"):
        run_eval.main(["run", "--target", "small", "--mock", "--dataset-csv",
                       str(data), "--out", str(large)])  # fmt: skip

    assert run_eval.main(["oracle", str(small), str(large)]) == 0
    assert "large model on 0/2" in capsys.readouterr().out


def test_runner_refuses_a_remote_gateway(tmp_path):
    run_eval = load_script()
    data = tmp_path / "fresh.csv"
    data.write_text("text,intent\nhello,card_arrival\n")
    with pytest.raises(SystemExit, match="Refusing example.org"):
        run_eval.main(["run", "--target", "gateway", "--base-url",
                       "http://example.org:8080", "--dataset-csv", str(data),
                       "--out", str(tmp_path / "g.jsonl")])  # fmt: skip
    assert not (tmp_path / "g.jsonl").exists()


def test_runner_needs_a_key_for_a_real_model(tmp_path, monkeypatch):
    run_eval = load_script()
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    data = tmp_path / "fresh.csv"
    data.write_text("text,intent\nhello,card_arrival\n")
    with pytest.raises(SystemExit, match="VLLM_API_KEY"):
        run_eval.main(["run", "--target", "small", "--base-url",
                       "http://10.0.0.1:8001", "--dataset-csv", str(data),
                       "--out", str(tmp_path / "s.jsonl")])  # fmt: skip


def test_macro_f1_scores_a_wrong_guess_like_unknown():
    # Same gold and the same accuracy: one run guesses intents that have no
    # gold messages, the other answers `unknown`. Both average over the gold
    # intents only, so neither is favoured.
    gold = ["card_arrival", "card_arrival", "age_limit", "age_limit"]
    guess = ["card_arrival", "atm_support", "age_limit", "exchange_rate"]
    abstain = ["card_arrival", "unknown", "age_limit", "unknown"]
    results = [
        score(
            [item(i, g, p) for i, (g, p) in enumerate(zip(gold, run, strict=True))],
            LABELS,
        )
        for run in (guess, abstain)
    ]
    assert [r["accuracy"] for r in results] == [0.5, 0.5]
    assert [r["macro_f1_classes"] for r in results] == [2, 2]
    # Each intent: precision 1, recall 1/2, F1 2/3.
    assert [r["macro_f1"] for r in results] == [pytest.approx(2 / 3)] * 2


def flaky_call(failures):
    """A call that raises the given exception the first time for each row."""
    seen = set()

    def make(backend, labels, cards):
        async def call(message):
            if message.row in failures and message.row not in seen:
                seen.add(message.row)
                raise failures[message.row]
            return {
                "latency_ms": 1.0,
                "model": backend.config.name,
                "format_ok": True,
                "format_errors": [],
                "predicted": message.gold,
            }

        return call

    return make


def write_csv(tmp_path, rows=3):
    data = tmp_path / "fresh.csv"
    golds = ["card_arrival", "age_limit", "atm_support"][:rows]
    data.write_text(
        "text,intent\n" + "".join(f"m{i},{g}\n" for i, g in enumerate(golds))
    )
    return data


def test_resume_retries_harness_failures_and_keeps_model_failures(
    tmp_path, monkeypatch, capsys
):
    run_eval = load_script()
    failures = {0: BackendTimeoutError("t"), 1: BackendOutputError("o")}
    monkeypatch.setattr(run_eval, "backend_call", flaky_call(failures))
    data, out = write_csv(tmp_path), tmp_path / "small.jsonl"
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    assert run_eval.main(args) == 0
    _, items = run_eval.read_results(out)
    assert [(i["error"], i.get("final")) for i in items] == [
        ("BackendTimeoutError", False),
        ("BackendOutputError", True),
        (None, None),
    ]
    assert all(i["model"] == "qwen3-1.7b" for i in items)
    assert "1 failed calls to retry" in capsys.readouterr().out

    # The rerun sends only the timed-out row; the model's bad output stays.
    assert run_eval.main(args) == 0
    _, items = run_eval.read_results(out)
    assert [i["error"] for i in items] == [None, "BackendOutputError", None]
    assert score(items, LABELS)["correct"] == 2


def test_oracle_handles_a_row_that_failed_on_both_targets(
    tmp_path, monkeypatch, capsys
):
    run_eval = load_script()
    out = {t: tmp_path / f"{t}.jsonl" for t in ("small", "large")}
    data = write_csv(tmp_path)
    for target in out:
        failures = {0: BackendOutputError("o")}
        monkeypatch.setattr(run_eval, "backend_call", flaky_call(failures))
        assert run_eval.main(["run", "--target", target, "--mock", "--dataset-csv",
                              str(data), "--out", str(out[target])]) == 0  # fmt: skip
    combined = tmp_path / "oracle.jsonl"
    args = ["oracle", str(out["small"]), str(out["large"]), "--out", str(combined)]
    assert run_eval.main(args) == 0
    assert "large model on 0/3" in capsys.readouterr().out
    header, items = run_eval.read_results(combined)
    assert set(header["git_commit"]) == {"small", "large"}
    assert items[0]["error"] == "BackendOutputError"


def test_resume_after_a_cut_off_last_line(tmp_path):
    run_eval = load_script()
    data, out = write_csv(tmp_path), tmp_path / "small.jsonl"
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    assert run_eval.main([*args, "--limit", "2"]) == 0
    lines = out.read_text().splitlines()
    # Lose the last row and leave half a line, as a kill mid-write would.
    out.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:40])
    assert len(run_eval.read_results(out)[1]) == 1
    assert run_eval.main(args) == 0
    header, items = run_eval.read_results(out)
    assert [i["row"] for i in items] == [0, 1, 2]
    assert all(line.endswith("}") for line in out.read_text().splitlines())
    # A complete last line that only lost its newline is kept, not cut.
    out.write_text(out.read_text().rstrip("\n"))
    assert run_eval.main(args) == 0
    assert [i["row"] for i in run_eval.read_results(out)[1]] == [0, 1, 2]
    assert out.read_text().endswith("}\n")
    # A broken line anywhere but the end is corruption, not a cut-off write.
    out.write_text(out.read_text().replace('"type": "item"', "{", 1))
    with pytest.raises(SystemExit, match="line 2 is not valid JSON"):
        run_eval.read_results(out)


def test_resume_refuses_a_different_commit(tmp_path, monkeypatch):
    run_eval = load_script()
    data, out = write_csv(tmp_path), tmp_path / "small.jsonl"
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    monkeypatch.setattr(run_eval, "git_commit", lambda: "aaa")
    assert run_eval.main([*args, "--limit", "1"]) == 0
    monkeypatch.setattr(run_eval, "git_commit", lambda: "bbb")
    with pytest.raises(SystemExit, match="started at commit aaa"):
        run_eval.main(args)


@pytest.mark.parametrize(
    ("status", "code", "final"),
    [
        (503, "unavailable", True),
        (422, "HTTP422", True),
        (413, "HTTP413", True),
        # About the harness or the stack, not the message: sent again.
        (401, "HTTPStatusError", False),
        (404, "HTTPStatusError", False),
        (429, "HTTPStatusError", False),
        (502, "HTTPStatusError", False),
    ],
)
def test_gateway_error_statuses(status, code, final):
    run_eval = load_script()

    def handler(request):
        return httpx.Response(status, json={"detail": "x"})

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            written = []
            messages = [run_eval.Message(0, "hi", "card_arrival")]
            await run_eval.run_items(
                messages, run_eval.gateway_call(client), written.append, 1
            )
            return written[0]

    result = asyncio.run(go())
    assert (result["error"], result["final"], result["predicted"]) == (
        code,
        final,
        None,
    )


def test_labelled_csv_keeps_one_split_with_file_row_numbers():
    body = (
        b"text,intent,split,source_row\n"
        b"a,card_arrival,train,0\n"
        b"b,age_limit,dev,1\n"
        b"c,atm_support,dev,2\n"
    )
    messages = parse_labelled_csv(body, LABELS, split="dev")
    assert [(m.row, m.gold) for m in messages] == [(1, "age_limit"), (2, "atm_support")]
    assert len(parse_labelled_csv(body, LABELS)) == 3  # no --split: every row
    with pytest.raises(ValueError, match="no split column"):
        parse_labelled_csv(b"text,intent\na,card_arrival\n", LABELS, split="dev")
    with pytest.raises(ValueError, match="No rows have split 'test'"):
        parse_labelled_csv(body, LABELS, split="test")


def test_runner_split_is_part_of_the_run(tmp_path):
    run_eval = load_script()
    data = tmp_path / "banking77_split.csv"
    data.write_text("text,intent,split\nm0,card_arrival,train\nm1,age_limit,dev\n")
    out = tmp_path / "dev.jsonl"
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    assert run_eval.main([*args, "--split", "dev"]) == 0
    header, items = run_eval.read_results(out)
    assert header["dataset"] == "banking77_split:dev" and header["dataset_size"] == 1
    assert [i["row"] for i in items] == [1]
    # The same file without --split is a different run, not a resume.
    with pytest.raises(SystemExit, match="different run"):
        run_eval.main(args)
    with pytest.raises(SystemExit):
        run_eval.main(["run", "--target", "small", "--mock", "--split", "dev",
                       "--out", str(out)])  # fmt: skip


def test_stratified_sample_covers_every_intent_and_repeats():
    messages = [
        Message(row, f"m{row}", gold)
        for row, gold in enumerate(["a"] * 6 + ["b"] * 6 + ["c"] * 6)
    ]
    sample = stratified_sample(messages, 7, seed=6700)
    golds = [m.gold for m in sample]
    assert len(sample) == 7
    assert sorted(golds.count(g) for g in "abc") == [2, 2, 3]
    assert [m.row for m in sample] == sorted(m.row for m in sample)
    assert sample == stratified_sample(messages, 7, seed=6700)
    assert sample != stratified_sample(messages, 7, seed=1)
    # Asking for more than there are returns them all.
    assert len(stratified_sample(messages, 100, seed=6700)) == 18
    with pytest.raises(ValueError, match="at least 1"):
        stratified_sample(messages, 0, seed=6700)


def test_runner_sample_is_its_own_run(tmp_path, capsys):
    run_eval = load_script()
    rows = [("card_arrival", 4), ("age_limit", 4), ("atm_support", 4)]
    data = tmp_path / "set.csv"
    data.write_text("text,intent\n" + "".join(
        f"m{gold}{i},{gold}\n" for gold, n in rows for i in range(n)))  # fmt: skip
    out = tmp_path / "s.jsonl"
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    assert run_eval.main([*args, "--sample", "6"]) == 0
    header, items = run_eval.read_results(out)
    assert header["sample"] == {"size": 6, "seed": 6700}
    assert sorted({i["gold"] for i in items}) == [
        "age_limit",
        "atm_support",
        "card_arrival",
    ]
    printed = capsys.readouterr().out
    assert "6/6 (sample of 12, seed 6700)" in printed and "incomplete" not in printed
    # Another seed, or no sample, is a different run.
    for other in (["--sample", "6", "--seed", "1"], []):
        with pytest.raises(SystemExit, match="different run"):
            run_eval.main([*args, *other])
    with pytest.raises(SystemExit):
        run_eval.main([*args, "--sample", "6", "--limit", "3"])


def test_direct_targets_refuse_what_the_gateway_refuses(tmp_path):
    run_eval = load_script()
    data, out = tmp_path / "edge.csv", tmp_path / "small.jsonl"
    data.write_text(
        "text,intent\n"
        '" ",card_arrival\n'
        f"{'x' * 2001},card_arrival\n"
        "  where is my card  ,card_arrival\n"
    )
    args = ["run", "--target", "small", "--mock", "--dataset-csv", str(data),
            "--out", str(out)]  # fmt: skip
    assert run_eval.main(args) == 0
    _, items = run_eval.read_results(out)
    assert [(i["error"], i.get("final")) for i in items] == [
        ("HTTP422", True),
        ("HTTP422", True),
        (None, None),
    ]


def test_resume_refuses_another_server(tmp_path, monkeypatch):
    run_eval = load_script()
    monkeypatch.setenv("VLLM_API_KEY", "k")
    data, out = write_csv(tmp_path), tmp_path / "small.jsonl"
    out.write_text(json.dumps({
        "type": "run", "dataset": "fresh", "dataset_sha256": sha256(data.read_bytes()),
        "dataset_size": 3, "target": "small", "base_url": "http://10.0.0.1:8001",
        "model": "qwen3-1.7b", "mocked": False, "sample": None,
        "option_set_version": LABELS.option_set_version, "git_commit": "x",
    }) + "\n")  # fmt: skip
    with pytest.raises(SystemExit, match="holds a different run"):
        run_eval.main(["run", "--target", "small", "--base-url",
                       "http://10.0.0.2:8001", "--dataset-csv", str(data),
                       "--out", str(out)])  # fmt: skip


def test_oracle_on_different_rows_is_a_clear_error(tmp_path):
    run_eval = load_script()
    data = write_csv(tmp_path)
    out = {t: tmp_path / f"{t}.jsonl" for t in ("small", "large")}
    for target, limit in (("small", "2"), ("large", "3")):
        assert run_eval.main(["run", "--target", target, "--mock", "--dataset-csv",
                              str(data), "--out", str(out[target]),
                              "--limit", limit]) == 0  # fmt: skip
    with pytest.raises(SystemExit, match="only in the large results: run both"):
        run_eval.main(["oracle", str(out["small"]), str(out["large"])])


def direct(row, gold, raw, errors=()):
    """A direct-target item: the model said `raw`; a failed check delivers
    unknown."""
    return item(
        row,
        gold,
        "unknown" if errors else raw,
        raw_intent=raw,
        format_ok=not errors,
        format_errors=list(errors),
    )


DIRECT = [
    direct(0, "card_arrival", "card_arrival"),
    # Right intent, but the check failed: the customer saw the apology.
    direct(1, "exchange_rate", "exchange_rate", ["unknown_policy_facts"]),
    direct(2, "exchange_rate", "top_up_failed", ["missing_policy_facts"]),
    direct(3, "top_up_failed", "unknown", ["unknown_policy_facts"]),
    {**item(4, "top_up_failed", None), "raw_intent": None, "error": "X", "final": True},
]


def test_score_shows_what_the_format_check_cost():
    result = score(DIRECT, LABELS)
    assert result["accuracy"] == 0.2
    assert result["intent_accuracy_before_check"] == 0.4
    assert result["lost_to_format_check"] == 1
    assert result["format_errors_by_type"] == {
        "unknown_policy_facts": 2,
        "missing_policy_facts": 1,
    }


def test_gateway_results_have_no_before_check_accuracy():
    items = [item(0, "card_arrival", "unknown", format_ok=False,
                  format_errors=["unknown_policy_facts"])]  # fmt: skip
    result = score(items, LABELS)
    assert result["intent_accuracy_before_check"] is None
    assert result["lost_to_format_check"] is None
    assert result["format_errors_by_type"] == {"unknown_policy_facts": 1}


def test_confusions_use_the_models_own_answer():
    # Row 1 was right before the check, so it is not a confusion; the gateway
    # target (no raw_intent) falls back to the delivered intent.
    assert confusions(DIRECT, 10) == [
        ("exchange_rate", "top_up_failed", 1),
        ("top_up_failed", "unknown", 1),
        ("top_up_failed", None, 1),
    ]
    delivered = [{k: v for k, v in i.items() if k != "raw_intent"} for i in DIRECT]
    assert ("exchange_rate", "unknown", 2) in confusions(delivered, 10)


def test_score_command_prints_the_breakdown(tmp_path, capsys):
    run_eval = load_script()
    out = tmp_path / "small.jsonl"
    header = {"type": "run", "dataset": "fresh", "dataset_size": 5,
              "target": "small", "model": "qwen3-1.7b", "sample": None}  # fmt: skip
    lines = [header] + [{"type": "item", **i} for i in DIRECT]
    out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert run_eval.main(["score", str(out), "--top", "2"]) == 0
    text = capsys.readouterr().out
    assert (
        "format errors by type {'unknown_policy_facts': 2, 'missing_policy_facts': 1}"
        in text
    )
    assert "intent accuracy before the format check 0.400" in text
    assert "right intents lost to the check 1" in text
    assert "lowest recall: exchange_rate 0.00, top_up_failed 0.00" in text
    assert "exchange_rate -> top_up_failed  1" in text
    assert run_eval.main(["score", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)[str(out)]
    assert report["score"]["lost_to_format_check"] == 1
    assert len(report["confusions"]) == 3
