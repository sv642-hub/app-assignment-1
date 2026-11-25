"""Every series and label an alert uses is one the gateway serves, exactly as
Prometheus stores it, so a renamed metric or label can't silently turn an
alert off. promtool tests the alert logic in prometheus/alerts_test.yml."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.router import MockJevRouter

RULES = Path("prometheus/alerts.yml")
RUNBOOK = Path("docs/MONITORING.md")
RUNBOOK_URL = (
    "https://github.com/sv642-hub/app-assignment-1/blob/main/docs/MONITORING.md#"
)
SERIES = re.compile(r"\b(gateway_[a-z_]+)\b(?:\{([^}]*)\})?")
MATCHER = re.compile(r"(\w+)\s*(?:=~|!~|!=|=)")
GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
TEMPLATE_LABEL = re.compile(r"\$labels\.(\w+)")


class FailingJev(MockJevRouter):
    async def route(self, message: str):
        raise ConnectionError("Jev is down")


def served_series() -> dict[str, set[str]]:
    """Each sample name on /metrics, with its label names. Traffic first, so
    labelled series that appear only after an event (a reply, a Jev
    fallback) are there too."""
    settings = Settings(_env_file=None)
    served: dict[str, set[str]] = {}
    for router in (
        MockJevRouter(settings.intents_path),
        FailingJev(settings.intents_path),
    ):
        with TestClient(create_app(settings, router)) as client:
            client.post("/v1/chat", json={"message": "my card has not arrived"})
            text = client.get("/metrics").text
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                served.setdefault(sample.name, set()).update(sample.labels)
    return served


def problems(rules: list[dict[str, Any]], served: dict[str, set[str]]) -> list[str]:
    """What in the rules Prometheus would never find in the gateway's series."""
    found = []
    for rule in rules:
        name, expr = rule["alert"], rule["expr"]
        labels: set[str] = set()
        for metric, matchers in SERIES.findall(expr):
            if metric not in served:
                found.append(f"{name}: no series {metric}")
                continue
            labels |= served[metric]
            for label in MATCHER.findall(matchers):
                if label not in served[metric]:
                    found.append(f"{name}: {metric} has no label {label}")
        if not labels:
            continue
        for group in GROUPING.findall(expr):
            for label in filter(None, (x.strip() for x in group.split(","))):
                if label not in labels:
                    found.append(f"{name}: no metric here has the label {label}")
        text = " ".join(str(v) for v in rule.get("annotations", {}).values())
        for label in TEMPLATE_LABEL.findall(text):
            if label not in labels | set(rule.get("labels", {})):
                found.append(f"{name}: annotations use $labels.{label}")
    return found


def rules() -> list[dict[str, Any]]:
    return yaml.safe_load(RULES.read_text())["groups"][0]["rules"]


def test_alert_rules_only_use_served_series_and_labels():
    assert problems(rules(), served_series()) == []


@pytest.mark.parametrize(
    ("expr", "annotation"),
    [
        # A typo in a metric name.
        ("gateway_model_disabeld == 1", ""),
        # A counter without _total: Prometheus stores only the _total series.
        ("increase(gateway_chat_unavailable[5m]) > 0", ""),
        # A histogram's base name instead of its _bucket series.
        ("rate(gateway_chat_latency_seconds[5m]) > 1", ""),
        # A label the gateway never serves.
        ('gateway_model_disabled{backend="qwen3-1.7b"} == 1', ""),
        # Grouping by a label the histogram lacks.
        (
            "histogram_quantile(0.95, sum by (bucket) "
            "(rate(gateway_chat_latency_seconds_bucket[5m]))) > 10",
            "",
        ),
        # An annotation naming a label the series lack.
        ("gateway_model_disabled == 1", "{{ $labels.backend }} is out"),
    ],
)
def test_a_rule_prometheus_could_never_match_is_caught(expr, annotation):
    rule = {"alert": "Mutated", "expr": expr, "annotations": {"summary": annotation}}
    assert problems([rule], served_series()) != []


def test_every_alert_has_a_severity_and_a_summary():
    for rule in rules():
        assert rule["labels"]["severity"] in ("warning", "critical"), rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]


def test_prometheus_loads_the_rules_file():
    config = yaml.safe_load(Path("prometheus/prometheus.yml").read_text())
    assert config["rule_files"] == ["alerts.yml"]
    compose = Path("compose.yml").read_text()
    assert "./prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro" in compose


def test_every_alert_links_to_its_runbook_section():
    # GitHub turns the heading "### GatewayDown" into the anchor #gatewaydown.
    text = RUNBOOK.read_text()
    alerts_part = text.split("\n## Alerts\n", 1)[1].split("\n## ", 1)[0]
    headings = re.findall(r"^### (\S+)$", alerts_part, re.M)
    assert headings == [rule["alert"] for rule in rules()]
    for rule in rules():
        assert rule["annotations"]["runbook_url"] == RUNBOOK_URL + rule["alert"].lower()
