from pathlib import Path

from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings


def test_recent_requests_reports_actual_fallback_and_bounds():
    settings = Settings(
        _env_file=None,
        backends_path=Path("config/backends.outage.json"),
        request_store_limit=2,
    )
    with TestClient(create_app(settings)) as c:
        responses = [
            c.post("/v1/chat", json={"message": "card arrival"}).json()
            for _ in range(3)
        ]
        recent = c.get("/v1/health").json()["recent_requests"]
        assert [r["id"] for r in recent] == [responses[2]["id"], responses[1]["id"]]
        assert all(r["model"] == "qwen3-4b" and r["route"] == "complex" for r in recent)
        assert all(r["fallback_reason"] == "selected_backend_error" for r in recent)
        assert all(r["completed_at"].endswith("Z") for r in recent)
        assert all("reply" not in r and "message" not in r for r in recent)
    with TestClient(create_app(settings)) as c:
        assert c.get("/v1/health").json()["recent_requests"] == []
