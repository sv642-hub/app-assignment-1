"""scripts/smoke_compose.py against a fake stack in each mode."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_compose.py"
MOCK_INTENTS = {
    "My card has not arrived": ("card_arrival", "standard"),
    "I lost my card": ("lost_or_stolen_card", "complex"),
    "What is the weather?": ("unknown", "standard"),
}
MODELS = {"standard": "qwen3-1.7b", "complex": "qwen3-4b"}


def stack(jev="mock", qwen="mock", router=None, route=None, model=None, intent=None):
    """A fake proxy. Real Jev sends every message to `route` (default complex)."""
    stored = {}

    def chat(message):
        mock_intent, mock_route = MOCK_INTENTS[message]
        chosen = mock_route if jev == "mock" else route or "complex"
        reply_intent = intent or mock_intent
        result = {
            "id": str(len(stored)),
            "intent": reply_intent,
            "trace": {
                "router": router or ("mock_jev" if jev == "mock" else "jev"),
                "route": chosen,
                "model": model or MODELS[chosen],
                "mocked": qwen == "mock",
                "format_check": "failed" if qwen == "real" else "passed",
                "reply_source": "no_match" if reply_intent == "unknown" else "x",
                "fallback_reason": "jev_timeout" if router == "rule_router" else None,
                "latency_ms": 1.0,
            },
        }
        stored[result["id"]] = result
        return result

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/metrics"):
                self.send(b'<div id="root"></div>', [("Cache-Control", "no-cache")])
            elif self.path == "/v1/health":
                backends = [{"name": n, "route": r} for r, n in MODELS.items()]
                self.send({"controller": {}, "mode": qwen, "jev_mode": jev,
                           "backends": backends})  # fmt: skip
            elif self.path == "/grafana/api/health":
                self.send({"database": "ok"})
            elif self.path.startswith("/grafana/api/dashboards/"):
                self.send({"meta": {"canSave": False}})
            elif self.path.startswith("/v1/requests/"):
                self.send(stored[self.path.rsplit("/", 1)[1]])
            else:
                self.send(b"ok")

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            self.send(chat(json.loads(self.rfile.read(length))["message"]))

    return Handler


def run(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        return subprocess.run(
            [sys.executable, str(SCRIPT), url], capture_output=True, text=True
        )
    finally:
        server.shutdown()
        server.server_close()


def test_all_mock_stack_passes():
    done = run(stack())
    assert done.returncode == 0, done.stderr
    assert "Stack: Jev mock, Qwen mock" in done.stdout
    assert "PASS: lost_or_stolen_card -> qwen3-4b via mock_jev" in done.stdout


@pytest.mark.parametrize("route", ["standard", "complex"])
def test_real_jev_passes_on_either_route(route):
    done = run(stack(jev="real", route=route))
    assert done.returncode == 0, done.stderr
    assert "Stack: Jev real, Qwen mock" in done.stdout
    expected = f"PASS: card_arrival -> {MODELS[route]} via jev, {route} route"
    assert expected in done.stdout


def test_real_jev_that_fell_back_to_the_rule_router_fails():
    done = run(stack(jev="real", router="rule_router"))
    assert done.returncode != 0
    assert "expected real Jev, got rule_router: jev_timeout" in done.stderr


def test_real_jev_with_the_wrong_model_for_the_route_fails():
    done = run(stack(jev="real", route="complex", model="qwen3-1.7b"))
    assert done.returncode != 0


def test_mock_jev_on_a_real_jev_check_fails():
    # The stack says real Jev, but the replies came from the mock router.
    done = run(stack(jev="real", router="mock_jev"))
    assert done.returncode != 0


def test_real_qwen_prints_a_wrong_intent_instead_of_failing():
    done = run(stack(jev="real", qwen="real", intent="exchange_rate"))
    assert done.returncode == 0, done.stderr
    assert "PASS: exchange_rate (expected card_arrival) -> qwen3-4b" in done.stdout
    assert "format failed" in done.stdout


def test_mock_qwen_with_a_wrong_intent_fails():
    done = run(stack(intent="exchange_rate"))
    assert done.returncode != 0
