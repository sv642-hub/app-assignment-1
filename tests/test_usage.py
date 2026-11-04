from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings


def test_usage_survives_window_and_counts_fallbacks():
    settings = Settings(_env_file=None, monitor_min_samples=30, monitor_window=30)
    with TestClient(create_app(settings)) as c:
        gateway = c.app.state.gateway
        for b in gateway.backends:
            b.config.latency_ms = 0
        gateway.backends[0].config.error_rate = 1
        for _ in range(35):
            assert (
                c.post("/v1/chat", json={"message": "card arrival"}).status_code == 200
            )
        health = c.get("/v1/health").json()
        usage = {u["name"]: u for u in health["usage"]}
        small, large = usage["qwen3-1.7b"], usage["qwen3-4b"]
        assert small["attempted_calls"] == small["failed_calls"] == 30
        assert small["completed_calls"] == 0
        assert large["attempted_calls"] == large["completed_calls"] == 35
        assert large["failed_calls"] == 0
        assert all(
            u["input_tokens"] is None and u["estimated_cost_usd"] is None
            for u in usage.values()
        )
        assert (
            next(m for m in health["monitor"] if m["name"] == "qwen3-4b")["samples"]
            == 30
        )
        gateway.backends[1].config.error_rate = 1
        assert c.post("/v1/chat", json={"message": "card arrival"}).status_code == 503
        usage = {u["name"]: u for u in c.get("/v1/health").json()["usage"]}
        assert usage["qwen3-4b"]["failed_calls"] == 1
    with TestClient(create_app(settings)) as c:
        assert all(
            u["attempted_calls"] == 0 for u in c.get("/v1/health").json()["usage"]
        )
