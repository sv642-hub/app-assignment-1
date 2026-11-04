from pathlib import Path

from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings


def test_documented_six_requests_really_inject_and_recover():
    settings = Settings(
        _env_file=None, backends_path=Path("config/backends.chaos.json")
    )
    with TestClient(create_app(settings)) as c:
        for i in range(6):
            result = c.post(
                "/v1/chat", json={"message": "My card has not arrived"}
            ).json()
            trace = result["trace"]
            if i < 5:
                assert trace["format_check"] == "failed"
                assert "unfilled_placeholder" in trace["format_errors"]
                assert "{customer_name}" not in result["reply"]
                assert bool(trace["controller_action"]) == (i == 4)
            else:
                assert trace["model"] == "qwen3-4b"
                assert trace["format_check"] == "passed"
                assert trace["fallback_reason"] == "selected_backend_disabled"
        small = c.get("/v1/health").json()["monitor"][0]
        assert small["format_failures"] == 5 and small["disabled_by_controller"]
    with TestClient(create_app(Settings(_env_file=None))) as c:
        trace = c.post("/v1/chat", json={"message": "My card has not arrived"}).json()[
            "trace"
        ]
        assert trace["format_check"] == "passed" and trace["model"] == "qwen3-1.7b"
