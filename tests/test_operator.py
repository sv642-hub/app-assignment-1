"""Operator routes: the token, disable and enable a model, and faults on command."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.monitor import Controller
from app_assignment_1.store import MemoryStore

TOKEN = "operator-token-for-tests-only-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
STANDARD = "my card has not arrived"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    values = {"operator_token": TOKEN, "controller_cooldown_seconds": 30} | overrides
    return Settings(_env_file=None, backends_path=path, **values)  # type: ignore[arg-type]


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(100)


@pytest.fixture
def client(tmp_path: Path, store: MemoryStore):
    with TestClient(create_app(make_settings(tmp_path), store=store)) as client:
        yield client


def monitor(client: TestClient) -> dict[str, dict]:
    return {m["name"]: m for m in client.get("/v1/health").json()["monitor"]}


# --- the token ---

ROUTES = [
    ("/v1/operator/models/qwen3-1.7b/disable", None),
    ("/v1/operator/models/qwen3-1.7b/enable", None),
    ("/v1/faults", {"model": SMALL, "fault": "outage"}),
]


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_operator_routes_are_off_without_a_token(tmp_path, path, body):
    settings = make_settings(tmp_path, operator_token=None)
    with TestClient(create_app(settings)) as client:
        response = client.post(path, json=body, headers=AUTH)
        assert response.status_code == 403
        assert "APP_OPERATOR_TOKEN" in response.json()["detail"]
        assert client.get("/v1/health").json()["status"] == "ok"


@pytest.mark.parametrize(("path", "body"), ROUTES)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
    ids=["missing", "wrong", "not-bearer"],
)
def test_operator_routes_need_the_token(client, path, body, headers):
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert client.get("/v1/health").json()["status"] == "ok"
    assert all(
        b["error_rate"] == 0 for b in client.get("/v1/health").json()["backends"]
    )


def test_operator_token_must_be_long():
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings(_env_file=None, operator_token="x" * 15)
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings(_env_file=None, operator_token=" " * 10 + "x" * 15 + " " * 10)
    assert Settings(_env_file=None, operator_token="x" * 16).operator_token
    settings = Settings(_env_file=None, operator_token=TOKEN)
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize("blank", ["", " " * 32, "\t\n"])
def test_a_blank_operator_token_turns_the_routes_off(blank):
    assert Settings(_env_file=None, operator_token=blank).operator_token is None


def test_spaces_around_the_operator_token_are_dropped(tmp_path):
    settings = make_settings(tmp_path, operator_token=f"  {TOKEN}  ")
    assert settings.operator_token is not None
    assert settings.operator_token.get_secret_value() == TOKEN
    with TestClient(create_app(settings)) as client:
        response = client.post(f"/v1/operator/models/{SMALL}/disable", headers=AUTH)
        assert response.status_code == 200


def test_the_bearer_scheme_is_case_insensitive(client):
    headers = {"Authorization": f"bearer {TOKEN}"}
    response = client.post(f"/v1/operator/models/{SMALL}/disable", headers=headers)
    assert response.status_code == 200


# --- disable and enable ---


def test_disable_holds_a_model_and_traffic_moves(client, store):
    response = client.post(
        f"/v1/operator/models/{SMALL}/disable",
        json={"reason": "draining for a deploy"},
        headers=AUTH,
    )
    assert response.status_code == 200
    held = {m["name"]: m for m in response.json()}[SMALL]
    assert held["disabled_by_operator"] is True
    assert held["disabled_by_controller"] is False

    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert (trace["jev_choice"], trace["model"]) == ("standard", LARGE)
    assert trace["fallback_reason"] == "selected_backend_disabled"

    health = client.get("/v1/health").json()
    assert health["status"] == "degraded"
    assert [(i["action"], i["reason"]) for i in health["interventions"]] == [
        ("operator_disabled", "draining for a deploy")
    ]
    assert [i.action for i in store.interventions] == ["operator_disabled"]


def test_enable_lifts_the_hold(client, store):
    client.post(f"/v1/operator/models/{SMALL}/disable", headers=AUTH)
    response = client.post(f"/v1/operator/models/{SMALL}/enable", headers=AUTH)
    assert response.status_code == 200
    assert not {m["name"]: m for m in response.json()}[SMALL]["disabled_by_operator"]
    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert (trace["model"], trace["fallback_reason"]) == (SMALL, None)
    assert client.get("/v1/health").json()["status"] == "ok"
    assert [i.action for i in store.interventions] == [
        "operator_disabled",
        "operator_enabled",
    ]


def test_disable_never_takes_the_last_usable_model(client, store):
    client.post(f"/v1/operator/models/{SMALL}/disable", headers=AUTH)
    response = client.post(f"/v1/operator/models/{LARGE}/disable", headers=AUTH)
    assert response.status_code == 409
    assert monitor(client)[LARGE]["disabled_by_operator"] is False
    assert client.post("/v1/chat", json={"message": STANDARD}).status_code == 200
    assert [i.action for i in store.interventions] == ["operator_disabled"]


def test_repeated_disable_and_enable_log_once(client, store):
    for _ in range(2):
        client.post(f"/v1/operator/models/{SMALL}/disable", headers=AUTH)
    for _ in range(2):
        client.post(f"/v1/operator/models/{SMALL}/enable", headers=AUTH)
    assert [i.action for i in store.interventions] == [
        "operator_disabled",
        "operator_enabled",
    ]


def test_unknown_model_is_rejected(client):
    response = client.post("/v1/operator/models/gpt-5/disable", headers=AUTH)
    assert response.status_code == 422


def test_operator_actions_are_counted_in_metrics(client):
    client.post(f"/v1/operator/models/{SMALL}/disable", headers=AUTH)
    text = client.get("/metrics").text
    assert (
        'gateway_controller_interventions_total{action="operator_disabled",'
        'model="qwen3-1.7b"} 1.0'
    ) in text


def test_a_hold_has_no_cooldown(tmp_path):
    clock = Clock()
    c = Controller([SMALL, LARGE], make_settings(tmp_path), clock)
    c.operator_disable(SMALL, "test")
    clock.now = 10_000
    assert c.is_disabled(SMALL)
    c.operator_enable(SMALL, "test")
    assert not c.is_disabled(SMALL)


def test_enable_also_ends_a_controller_cooldown(tmp_path):
    clock = Clock()
    settings = make_settings(tmp_path, monitor_window=4, monitor_min_samples=3)
    c = Controller([SMALL, LARGE], settings, clock)
    for _ in range(3):
        c.record(SMALL, True, can_disable=True)
    assert c.is_disabled(SMALL)
    c.operator_enable(SMALL, "fixed by hand")
    assert not c.is_disabled(SMALL)
    stats = {s.name: s for s in c.stats()}[SMALL]
    assert (stats.samples, stats.disabled_by_controller) == (0, False)
    assert [i.action for i in c.log] == ["disabled", "operator_enabled"]


def test_a_cooldown_that_ends_under_a_hold_still_logs(tmp_path):
    clock = Clock()
    settings = make_settings(tmp_path, monitor_window=4, monitor_min_samples=3)
    c = Controller([SMALL, LARGE], settings, clock)
    for _ in range(3):
        c.record(SMALL, True, can_disable=True)
    c.operator_disable(SMALL, "test")
    clock.now = 30
    assert c.is_disabled(SMALL)  # the hold remains
    stats = {s.name: s for s in c.stats()}[SMALL]
    assert (stats.disabled_by_controller, stats.disabled_by_operator) == (False, True)
    assert [i.action for i in c.log] == ["disabled", "operator_disabled", "re-enabled"]


# --- faults on command ---


def test_outage_fault_moves_traffic(client):
    response = client.post(
        "/v1/faults", json={"model": SMALL, "fault": "outage"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["error_rate"] == 1
    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert (trace["model"], trace["fallback_reason"]) == (
        LARGE,
        "selected_backend_error",
    )
    backends = {b["name"]: b for b in client.get("/v1/health").json()["backends"]}
    assert backends[SMALL]["error_rate"] == 1


def test_bad_format_fault_corrupts_replies(client):
    client.post(
        "/v1/faults", json={"model": SMALL, "fault": "bad_format"}, headers=AUTH
    )
    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert trace["format_check"] == "failed"
    assert trace["format_errors"] == ["unknown_policy_facts"]


def test_label_swap_fault_and_partial_rate(client):
    response = client.post(
        "/v1/faults",
        json={"model": LARGE, "fault": "label_swap", "rate": 0.25},
        headers=AUTH,
    )
    assert response.json()["label_swap_rate"] == 0.25


def test_none_clears_every_fault(client):
    faults = [{"fault": "outage"}, {"fault": "bad_format"}, {"fault": "label_swap"},
              {"fault": "slow", "seconds": 5}, {"fault": "bad_prompt"}]  # fmt: skip
    for fault in faults:
        client.post("/v1/faults", json={"model": SMALL, **fault}, headers=AUTH)
    response = client.post(
        "/v1/faults", json={"model": SMALL, "fault": "none"}, headers=AUTH
    )
    body = response.json()
    rates = ("error_rate", "fault_rate", "label_swap_rate", "slow_rate",
             "slow_seconds", "bad_prompt_rate")  # fmt: skip
    assert [body[key] for key in rates] == [0] * len(rates)
    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert (trace["model"], trace["format_check"]) == (SMALL, "passed")


@pytest.mark.parametrize(
    "body",
    [
        {"model": "gpt-5", "fault": "outage"},
        {"model": SMALL, "fault": "fire"},
        {"model": SMALL, "fault": "outage", "rate": 1.5},
        {"model": SMALL, "fault": "outage", "extra": 1},
        {"model": SMALL, "fault": "slow"},  # slow needs seconds
        {"model": SMALL, "fault": "slow", "seconds": 0},
        {"model": SMALL, "fault": "slow", "seconds": 121},
        {"model": SMALL, "fault": "outage", "seconds": 5},  # only slow takes them
        {"model": SMALL, "fault": "outage", "seconds": None},  # not even as null
    ],
)
def test_bad_fault_requests_are_rejected(client, body):
    assert client.post("/v1/faults", json=body, headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        f'{{"model": "{SMALL}", "fault": "slow", "seconds": NaN}}',
        f'{{"model": "{SMALL}", "fault": "slow", "seconds": Infinity}}',
        f'{{"model": "{SMALL}", "fault": "slow", "seconds": 5, "rate": NaN}}',
        f'{{"model": "{SMALL}", "fault": "outage", "rate": Infinity}}',
    ],
)
def test_nan_and_infinity_are_a_422_not_a_500(client, body):
    headers = AUTH | {"content-type": "application/json"}
    assert client.post("/v1/faults", content=body, headers=headers).status_code == 422


# --- slow and bad_prompt (docs/CHAOS_SCENARIOS.md, G1 to G3) ---


def test_slow_fault_within_the_budget_only_adds_latency(client):
    response = client.post(
        "/v1/faults",
        json={"model": SMALL, "fault": "slow", "seconds": 0.2},
        headers=AUTH,
    )
    assert (response.json()["slow_rate"], response.json()["slow_seconds"]) == (1, 0.2)
    trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
    assert (trace["model"], trace["format_check"], trace["fallback_reason"]) == (
        SMALL,
        "passed",
        None,
    )
    assert trace["latency_ms"] >= 200


def test_hung_model_is_cut_off_and_the_other_model_answers(tmp_path, store):
    # The first model gets the deadline minus the reserve (about 0.5 s here);
    # the hung small model is cut off, and the large model answers in the
    # reserve. It counts as a failed call for the small model.
    settings = make_settings(
        tmp_path,
        jev_timeout_seconds=0.05,
        request_deadline_seconds=1.0,
        alternate_reserve_seconds=0.5,
    )
    with TestClient(create_app(settings, store=store)) as client:
        client.post(
            "/v1/faults",
            json={"model": SMALL, "fault": "slow", "seconds": 5},
            headers=AUTH,
        )
        response = client.post("/v1/chat", json={"message": STANDARD})
        assert response.status_code == 200
        trace = response.json()["trace"]
        assert (trace["model"], trace["fallback_reason"]) == (
            LARGE,
            "selected_backend_error",
        )
        assert 400 <= trace["latency_ms"] < 1000
        assert monitor(client)[SMALL]["call_errors"] == 1


def test_bad_prompt_passes_the_format_check(client):
    client.post(
        "/v1/faults", json={"model": SMALL, "fault": "bad_prompt"}, headers=AUTH
    )
    body = client.post("/v1/chat", json={"message": STANDARD}).json()
    trace = body["trace"]
    assert body["intent"] == "unknown"
    assert (trace["model"], trace["format_check"], trace["reply_source"]) == (
        SMALL,
        "passed",
        "no_match",
    )
    # The failure monitor sees nothing wrong.
    assert monitor(client)[SMALL]["format_failures"] == 0


def test_bad_prompt_is_caught_only_with_semantic_actions(tmp_path, store):
    settings = make_settings(
        tmp_path,
        semantic_actions_enabled=True,
        semantic_baseline_disagreement_rate=0,
    )
    with TestClient(create_app(settings, store=store)) as client:
        # The large model must have answered for the small one to be disabled.
        assert (
            client.post("/v1/chat", json={"message": "my card was stolen"}).json()[
                "trace"
            ]["model"]
            == LARGE
        )
        client.post(
            "/v1/faults", json={"model": SMALL, "fault": "bad_prompt"}, headers=AUTH
        )
        actions = []
        for _ in range(15):
            trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
            actions.append(trace["controller_action"])
        assert any(a and a.startswith(f"disabled {SMALL}") for a in actions)
        assert monitor(client)[SMALL]["disabled_for"] == "semantic"


def test_bad_prompt_goes_unnoticed_with_semantic_actions_off(client):
    # The default: nothing disables a model that answers `unknown` to everything.
    client.post("/v1/chat", json={"message": "my card was stolen"})
    client.post(
        "/v1/faults", json={"model": SMALL, "fault": "bad_prompt"}, headers=AUTH
    )
    for _ in range(15):
        trace = client.post("/v1/chat", json={"message": STANDARD}).json()["trace"]
        assert trace["controller_action"] is None
    assert monitor(client)[SMALL]["disabled_by_controller"] is False
