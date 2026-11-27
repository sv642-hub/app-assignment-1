"""scripts/chaos_demo.py really drives all three faults against the gateway."""

import importlib.util
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import PUBLIC_DEMO_OPERATOR_TOKEN, Settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "chaos_demo.py"
spec = importlib.util.spec_from_file_location("chaos_demo", SCRIPT)
assert spec and spec.loader
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class ClientApi:
    """Same call shape as demo.Api, but in process."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def call(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        if payload is None:
            return self.client.get(path).json()
        headers = {"Authorization": "Bearer " + demo.DEMO_TOKEN}
        response = self.client.post(path, json=payload, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()


def demo_settings(cooldown: float = 0.5) -> Settings:
    # Mirrors compose.demo.yml, with a short cooldown so the test is quick.
    return Settings(
        _env_file=None,
        operator_token=demo.DEMO_TOKEN,
        semantic_baseline_disagreement_rate=0,
        semantic_actions_enabled=True,
        controller_cooldown_seconds=cooldown,
    )


@pytest.mark.parametrize("name", list(demo.SCENARIOS))
def test_each_scenario_disables_and_recovers(name: str) -> None:
    with TestClient(create_app(demo_settings())) as client:
        lines: list[str] = []
        result = demo.run_scenario(
            ClientApi(client), name, out=lines.append, sleep=lambda _s: time.sleep(0.2)
        )
        assert result["messages_to_disable"] >= 1
        assert result["seconds_to_recover"] > 0
        text = "\n".join(lines)
        assert "CONTROLLER: " in text
        # The demo's own messages are the only text it sends.
        assert demo.STANDARD not in text and demo.COMPLEX not in text


# The controller needs 5 failures (monitor_min_samples) and the semantic
# monitor 10 scored replies (semantic_min_samples), all failing, from an empty
# window.
EXPECTED_MESSAGES = {"bad_format": 5, "outage": 5, "label_swap": 10}


def test_each_scenario_starts_from_empty_windows() -> None:
    """Run back to back, as the demo does: replies left over from one scenario
    must not count in the next one's window."""
    with TestClient(create_app(demo_settings())) as client:
        api, lines = ClientApi(client), []
        for name in [*demo.SCENARIOS, *demo.SCENARIOS]:
            result = demo.run_scenario(
                api, name, out=lines.append, sleep=lambda _s: time.sleep(0.2)
            )
            assert result["messages_to_disable"] == EXPECTED_MESSAGES[name], name
        text = "\n".join(lines)
        assert "100% over last 5 calls" in text
        assert "on 100% of last 10 scored replies" in text
        assert "80%" not in text and "90%" not in text


def test_while_disabled_runs_before_the_model_is_back() -> None:
    with TestClient(create_app(demo_settings(cooldown=3))) as client:
        api, seen = ClientApi(client), []

        def check(seconds_left: float) -> None:
            seen.append(seconds_left)
            assert demo.monitor(api, demo.SMALL)["disabled_by_controller"]

        result = demo.run_scenario(
            api,
            "outage",
            out=lambda _line: None,
            sleep=lambda _s: time.sleep(0.2),
            while_disabled=check,
        )
        assert len(seen) == 1 and 0 < seen[0] <= 3
        # The script reads its clock just after the controller disables.
        assert result["seconds_to_recover"] >= 2.5


def row(model: str, value: str) -> dict[str, Any]:
    return {"metric": {"model": model}, "value": [0, value]}


def test_prometheus_waits_for_a_new_scrape() -> None:
    """A scrape from before the disable may show the previous scenario, so the
    queries wait for one that happened after this one started."""
    scrapes = iter(["100", "100", "100", "115"])
    asked: list[str] = []

    def run(query: str) -> list[dict[str, Any]]:
        asked.append(query)
        if query == demo.SCRAPED_QUERY:
            return [row(demo.SMALL, next(scrapes))]
        return [row(demo.SMALL, "1")]

    lines: list[str] = []
    demo.prometheus(10, out=lines.append, run=run, sleep=lambda _s: None)
    assert asked[:4] == [demo.SCRAPED_QUERY] * 4
    assert asked[4:] == demo.QUERIES
    assert f"    {{model={demo.SMALL}}} 1" in lines
    assert not any("values may be old" in line for line in lines)


def test_prometheus_gives_up_when_the_cooldown_ends() -> None:
    now = iter(range(100))
    lines: list[str] = []
    demo.prometheus(
        3,
        out=lines.append,
        run=lambda _q: [row(demo.SMALL, "100")],
        sleep=lambda _s: None,
        clock=lambda: next(now),
    )
    assert "  no new scrape before the cooldown ended; values may be old" in lines


def test_prometheus_reports_an_unreachable_server() -> None:
    def run(_query: str) -> list[dict[str, Any]]:
        raise demo.DemoError("could not reach Prometheus: no container")

    lines: list[str] = []
    demo.prometheus(10, out=lines.append, run=run, sleep=lambda _s: None)
    assert "  could not reach Prometheus: no container" in lines


def test_scenario_names_match_the_fault_kinds() -> None:
    assert {fault for fault, _ in demo.SCENARIOS.values()} == {
        "bad_format",
        "outage",
        "label_swap",
    }


def test_public_demo_token_is_refused_outside_local() -> None:
    assert demo.DEMO_TOKEN == PUBLIC_DEMO_OPERATOR_TOKEN
    for environment in ("staging", "production"):
        with pytest.raises(ValueError, match="public token"):
            Settings(
                _env_file=None,
                environment=environment,
                operator_token=PUBLIC_DEMO_OPERATOR_TOKEN,
            )
    Settings(_env_file=None, operator_token=PUBLIC_DEMO_OPERATOR_TOKEN)


def test_script_parses_on_python_3_9() -> None:
    import ast

    source = SCRIPT.read_text()
    ast.parse(source, feature_version=(3, 9))
    assert "from __future__ import annotations" in source


def test_demo_override_matches_the_token() -> None:
    override = (SCRIPT.parent.parent / "compose.demo.yml").read_text()
    assert demo.DEMO_TOKEN in override


def test_a_failed_run_clears_the_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app(demo_settings())) as client:
        api = ClientApi(client)
        monkeypatch.setattr(demo, "MAX_MESSAGES", 1)
        monkeypatch.setattr(demo, "Api", lambda _url, _token: api)
        code = demo.main(["http://unused", "--scenario", "label_swap"])
        assert code == 1
        # No fault left: a standard message is answered with its own intent.
        reply = client.post("/v1/chat", json={"message": demo.STANDARD}).json()
        assert reply["intent"] == "card_arrival"


def test_unreachable_gateway_says_demo_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert demo.main(["http://127.0.0.1:1", "--scenario", "outage"]) == 1
    err = capsys.readouterr().err
    assert "DEMO FAILED: cannot reach" in err
    assert "Traceback" not in err


def test_cleanup_tries_both_models_and_fails_the_run(monkeypatch, capsys) -> None:
    calls = []

    class FailingCleanupApi:
        def call(self, path, payload):
            calls.append((path, payload.get("model")))
            if len(calls) == 1:
                raise demo.DemoError("first cleanup request failed")

    monkeypatch.setattr(demo, "Api", lambda *_: FailingCleanupApi())
    monkeypatch.setattr(demo, "run_scenario", lambda *_, **__: {})
    assert demo.main(["--scenario", "outage"]) == 1
    assert calls == [
        ("/v1/faults", demo.SMALL),
        (f"/v1/operator/models/{demo.SMALL}/enable", None),
        ("/v1/faults", demo.LARGE),
        (f"/v1/operator/models/{demo.LARGE}/enable", None),
    ]
    assert "cleanup failed" in capsys.readouterr().err
