"""Run against the public proxy: python scripts/smoke_compose.py [base_url]."""

import json
import sys
from urllib.request import Request, urlopen

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def fetch(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=15) as response:
        return response.read()


assert b'id="root"' in fetch("/"), "React app not served at /"
for path in ["/docs", "/openapi.json", "/v1/health"]:
    assert fetch(path), path
assert "controller" in json.loads(fetch("/v1/health"))
for message, intent, model in [
    ("My card has not arrived", "card_arrival", "qwen3-1.7b"),
    ("I lost my card", "lost_or_stolen_card", "qwen3-4b"),
    ("What is the weather?", "unknown", "qwen3-1.7b"),
]:
    result = json.loads(fetch("/v1/chat", {"message": message}))
    assert result["intent"] == intent, result
    assert result["trace"]["model"] == model, result
    assert result["trace"]["format_check"] == "passed", result
    assert result["trace"]["mocked"] is True
    assert result["trace"]["router"] == "mock_jev"
    if intent == "unknown":
        assert result["trace"]["reply_source"] == "no_match"
    assert json.loads(fetch("/v1/requests/" + result["id"])) == result
    print(f"PASS: {intent} -> {model} ({result['trace']['latency_ms']} ms)")
print("PASS: frontend, docs, health, chat and history through " + base)
