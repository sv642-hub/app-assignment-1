"""Run against the public proxy: python scripts/smoke_compose.py [base_url].

Reads the modes from /v1/health and checks what each one should give, so it
passes on the all-mock stack (CI) and on the testers' stack (real Jev):

- Jev mock: the mock router picks the route, so the model is fixed per message.
- Jev real: TypeSafe picks the route. Each chat is one paid call. The reply must
  come from Jev, not the rule router, and from the model on Jev's route; which
  route Jev picks is not checked.
- Qwen mock: the intent and the format check are fixed per message.
- Qwen real: the model can be wrong, so the intent and the format check are
  printed, not checked.
"""

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
with urlopen(base + "/", timeout=15) as page:
    assert page.headers["Cache-Control"] == "no-cache", "index.html must be rechecked"
for path in ["/docs", "/openapi.json", "/v1/health"]:
    assert fetch(path), path
health = json.loads(fetch("/v1/health"))
assert "controller" in health
jev_real = health.get("jev_mode") == "real"
qwen_real = health.get("mode") == "real"
model_for_route = {b["route"]: b["name"] for b in health["backends"]}
print(
    f"Stack: Jev {'real' if jev_real else 'mock'}, "
    f"Qwen {'real' if qwen_real else 'mock'}"
)
# /metrics is internal: through the proxy it must be the React app, not metrics.
metrics_page = fetch("/metrics")
assert b"# TYPE" not in metrics_page and b"gateway_" not in metrics_page, "/metrics"
assert b'id="root"' in metrics_page, "/metrics should fall back to the React app"
# Grafana is reached only through the proxy, as a viewer: the dashboard loads
# and there is no login.
grafana = json.loads(fetch("/grafana/api/health"))
assert grafana["database"] == "ok", grafana
dash = json.loads(fetch("/grafana/api/dashboards/uid/smart-gateway"))
assert dash["meta"]["canSave"] is False, "anonymous visitors must not edit"
for message, intent, mock_model in [
    ("My card has not arrived", "card_arrival", "qwen3-1.7b"),
    ("I lost my card", "lost_or_stolen_card", "qwen3-4b"),
    ("What is the weather?", "unknown", "qwen3-1.7b"),
]:
    result = json.loads(fetch("/v1/chat", {"message": message}))
    trace = result["trace"]
    if jev_real:
        why = f"expected real Jev, got {trace['router']}: {trace['fallback_reason']}"
        assert trace["router"] == "jev", why
        assert trace["model"] == model_for_route[trace["route"]], result
    else:
        assert trace["router"] == "mock_jev", result
        assert trace["model"] == mock_model, result
    assert trace["mocked"] is not qwen_real, result
    if not qwen_real:
        assert result["intent"] == intent, result
        assert trace["format_check"] == "passed", result
        if intent == "unknown":
            assert trace["reply_source"] == "no_match", result
    assert json.loads(fetch("/v1/requests/" + result["id"])) == result
    got = "" if result["intent"] == intent else f" (expected {intent})"
    print(
        f"PASS: {result['intent']}{got} -> {trace['model']} via {trace['router']}, "
        f"{trace['route']} route, format {trace['format_check']} "
        f"({trace['latency_ms']} ms)"
    )
print("PASS: frontend, docs, health, chat and history through " + base)
