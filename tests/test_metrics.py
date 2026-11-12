"""Prometheus metrics at the internal /metrics path."""

import asyncio
import fnmatch
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 0
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    return Settings(
        _env_file=None,
        backends_path=path,
        monitor_window=4,
        monitor_min_samples=3,
        controller_cooldown_seconds=30,
    )


def samples(client: TestClient) -> dict[tuple[str, frozenset], float]:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return {
        (s.name, frozenset(s.labels.items())): s.value
        for family in text_string_to_metric_families(response.text)
        for s in family.samples
    }


def value(found: dict, name: str, **labels: str) -> float:
    return found[(name, frozenset(labels.items()))]


def test_chat_request_updates_counters(settings):
    with TestClient(create_app(settings)) as client:
        assert value(samples(client), "gateway_chat_latency_seconds_count") == 0
        client.post("/v1/chat", json={"message": "My card has not arrived"})
        client.post("/v1/chat", json={"message": "I lost my card"})
        found = samples(client)
        assert (
            value(
                found,
                "gateway_chat_requests_total",
                route="standard",
                model="qwen3-1.7b",
                reply_source="policy_template",
                format_check="passed",
            )
            == 1
        )
        assert value(found, "gateway_chat_latency_seconds_count") == 2
        assert value(found, "gateway_chat_unavailable_total") == 0
        for model in ["qwen3-1.7b", "qwen3-4b"]:
            assert value(found, "gateway_backend_calls_attempted_total", model=model)
            assert value(found, "gateway_backend_calls_completed_total", model=model)
            assert value(found, "gateway_backend_calls_failed_total", model=model) == 0
            assert value(found, "gateway_model_disabled", model=model) == 0
            assert value(found, "gateway_monitor_failure_rate", model=model) == 0
            assert value(found, "gateway_monitor_window_samples", model=model) == 1
        for reason in [
            "jev_timeout",
            "jev_error",
            "jev_invalid_route",
            "jev_circuit_open",
        ]:
            assert value(found, "gateway_jev_fallbacks_total", reason=reason) == 0


def test_backend_counters_match_health_usage(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.gateway.backends[0].config.error_rate = 1
        for _ in range(2):
            client.post("/v1/chat", json={"message": "My card has not arrived"})
        found = samples(client)
        for usage in client.get("/v1/health").json()["usage"]:
            for field in ["attempted", "completed", "failed"]:
                assert (
                    value(
                        found,
                        f"gateway_backend_calls_{field}_total",
                        model=usage["name"],
                    )
                    == usage[f"{field}_calls"]
                )
        assert value(found, "gateway_backend_calls_failed_total", model="qwen3-1.7b")


def test_controller_disable_shows_in_gauges_and_interventions(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.gateway.backends[0].config.fault_rate = 1
        for _ in range(3):
            client.post("/v1/chat", json={"message": "My card has not arrived"})
        found = samples(client)
        assert value(found, "gateway_model_disabled", model="qwen3-1.7b") == 1
        assert value(found, "gateway_model_disabled", model="qwen3-4b") == 0
        assert value(found, "gateway_monitor_failure_rate", model="qwen3-1.7b") == 1
        assert (
            value(
                found,
                "gateway_controller_interventions_total",
                action="disabled",
                model="qwen3-1.7b",
            )
            == 1
        )
        assert (
            value(
                found,
                "gateway_controller_interventions_total",
                action="re-enabled",
                model="qwen3-1.7b",
            )
            == 0
        )
        assert (
            value(
                found,
                "gateway_chat_requests_total",
                route="standard",
                model="qwen3-1.7b",
                reply_source="format_fallback",
                format_check="failed",
            )
            == 3
        )


def test_jev_fallbacks_by_reason(settings):
    class OfflineRouter:
        async def route(self, message):
            raise ConnectionError("offline")

    with TestClient(create_app(settings, OfflineRouter())) as client:
        client.post("/v1/chat", json={"message": "My card has not arrived"})
        found = samples(client)
        assert value(found, "gateway_jev_fallbacks_total", reason="jev_error") == 1
        assert value(found, "gateway_jev_fallbacks_total", reason="jev_timeout") == 0


def test_unavailable_requests_are_counted(settings):
    with TestClient(create_app(settings)) as client:
        for backend in client.app.state.gateway.backends:
            backend.config.available = False
        assert client.post("/v1/chat", json={"message": "card"}).status_code == 503
        found = samples(client)
        assert value(found, "gateway_chat_unavailable_total") == 1
        assert value(found, "gateway_chat_latency_seconds_count") == 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_re_enable_after_cooldown_is_counted_on_scrape(settings):
    with TestClient(create_app(settings)) as client:
        gateway = client.app.state.gateway
        clock = Clock()
        gateway.controller.clock = clock
        gateway.backends[0].config.fault_rate = 1
        for _ in range(3):
            client.post("/v1/chat", json={"message": "My card has not arrived"})
        assert value(samples(client), "gateway_model_disabled", model="qwen3-1.7b")
        clock.now = 30
        # The scrape alone notices the cooldown has ended.
        found = samples(client)
        assert value(found, "gateway_model_disabled", model="qwen3-1.7b") == 0
        assert value(found, "gateway_monitor_window_samples", model="qwen3-1.7b") == 0
        for action in ["disabled", "re-enabled"]:
            assert (
                value(
                    found,
                    "gateway_controller_interventions_total",
                    action=action,
                    model="qwen3-1.7b",
                )
                == 1
            )


def test_kept_last_backend_is_counted(settings):
    with TestClient(create_app(settings)) as client:
        for backend in client.app.state.gateway.backends:
            backend.config.fault_rate = 1
        for _ in range(6):
            client.post("/v1/chat", json={"message": "My card has not arrived"})
        found = samples(client)
        assert value(found, "gateway_model_disabled", model="qwen3-1.7b") == 1
        assert value(found, "gateway_model_disabled", model="qwen3-4b") == 0
        assert (
            value(
                found,
                "gateway_controller_interventions_total",
                action="kept_last_backend",
                model="qwen3-4b",
            )
            == 1
        )


def test_jev_timeout_has_its_own_label(settings):
    class SlowRouter:
        async def route(self, message):
            await asyncio.sleep(5)

    settings.jev_timeout_seconds = 0.05
    with TestClient(create_app(settings, SlowRouter())) as client:
        client.post("/v1/chat", json={"message": "My card has not arrived"})
        found = samples(client)
        assert value(found, "gateway_jev_fallbacks_total", reason="jev_timeout") == 1
        assert value(found, "gateway_jev_fallbacks_total", reason="jev_error") == 0


def test_message_reply_and_id_never_appear_in_metrics(settings):
    message = "Zebraquokka 4111 my card has not arrived"
    with TestClient(create_app(settings)) as client:
        result = client.post("/v1/chat", json={"message": message}).json()
        text = client.get("/metrics").text
    assert "Zebraquokka" not in text
    assert "4111" not in text
    assert result["reply"] not in text
    assert result["id"] not in text


def test_metrics_stay_out_of_v1_and_the_docs(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/metrics").status_code == 404
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]


METRICS_PATHS = ["/metrics", "/metrics/", "/metrics/x", "/metric", "/METRICS"]


def test_caddy_does_not_proxy_metrics():
    caddyfile = Path("Caddyfile").read_text()
    # The only reverse_proxy is the @gateway handle, so its matcher decides
    # what reaches the gateway.
    assert caddyfile.count("reverse_proxy") == 1
    matcher = re.search(r"@gateway path (.+)", caddyfile)
    assert matcher, "Caddyfile no longer has the @gateway matcher"
    patterns = matcher.group(1).split()
    # Caddy path patterns use * wildcards and match case-insensitively.
    proxied = [
        (pattern, path)
        for path in METRICS_PATHS
        for pattern in patterns
        if fnmatch.fnmatch(path.lower(), pattern.lower())
    ]
    assert proxied == []


@pytest.mark.parametrize(
    "line",
    ["/v1 /v1/* /metrics", "/v1 /v1/* /metric*", "/v1 /* /docs", "/v1 /M*"],
)
def test_caddy_check_catches_metrics_patterns(line):
    patterns = line.split()
    assert any(
        fnmatch.fnmatch(path.lower(), pattern.lower())
        for path in METRICS_PATHS
        for pattern in patterns
    )
