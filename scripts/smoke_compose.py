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


for path in ["/", "/_stcore/health", "/docs", "/openapi.json", "/v1/health"]:
    assert fetch(path), path
for message, intent, model in [
    ("My card has not arrived", "card_arrival", "qwen3-1.7b"),
    ("I lost my card", "lost_or_stolen_card", "qwen3-4b"),
    ("What is the weather?", "unknown", None),
]:
    result = json.loads(fetch("/v1/chat", {"message": message}))
    assert result["intent"] == intent, result
    assert result["trace"]["model"] == model, result
    assert result["trace"]["format_check"] == "passed", result
    assert result["trace"]["mocked"] is True
    if intent == "unknown":
        assert result["trace"]["router"] == "rule_router"
        assert result["trace"]["reply_source"] == "rule_router"
        assert result["trace"]["simulated_latency_ms"] == 0
    assert json.loads(fetch("/v1/requests/" + result["id"])) == result
    print(f"PASS: {intent} -> {model} ({result['trace']['latency_ms']} ms)")
print("PASS: frontend, docs, health, chat and history through " + base)
