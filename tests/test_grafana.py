"""The Grafana files: the dashboard only asks Prometheus for series the gateway
serves, it matches its generator, and Grafana stays viewer-only behind Caddy."""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "grafana/dashboards/smart-gateway.json"
SERIES = re.compile(r"\b(gateway_[a-z_]+)\b(?:\{([^}]*)\})?")
MATCHER = re.compile(r"(\w+)\s*(?:=~|!~|!=|=)")
GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
LEGEND_LABEL = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text())


def served() -> dict[str, set[str]]:
    settings = Settings(_env_file=None, jev_input_price_per_million_usd=1)
    with TestClient(create_app(settings)) as client:
        client.post("/v1/chat", json={"message": "my card has not arrived"})
        text = client.get("/metrics").text
    out: dict[str, set[str]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out.setdefault(sample.name, set()).update(sample.labels)
    return out


def prometheus_targets() -> list[dict]:
    return [
        target
        for panel in dashboard()["panels"]
        for target in panel.get("targets", [])
        if target["datasource"]["type"] == "prometheus"
    ]


def test_dashboard_matches_its_generator():
    result = subprocess.run(
        [sys.executable, "scripts/build_grafana_dashboard.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_panels_use_only_series_and_labels_the_gateway_serves():
    series = served()
    problems = []
    for target in prometheus_targets():
        expr = target["expr"]
        labels: set[str] = set()
        for metric, matchers in SERIES.findall(expr):
            if metric not in series:
                problems.append(f"no series {metric}")
                continue
            labels |= series[metric]
            for label in MATCHER.findall(matchers):
                if label not in series[metric]:
                    problems.append(f"{metric} has no label {label}")
        for group in GROUPING.findall(expr):
            for label in filter(None, (x.strip() for x in group.split(","))):
                if label not in labels:
                    problems.append(f"{expr}: no label {label}")
        for label in LEGEND_LABEL.findall(target["legendFormat"]):
            if label not in labels:
                problems.append(f"legend uses label {label}: {expr}")
    assert problems == []


def test_latency_target_buckets_exist():
    # The SLO panels count requests under 2 s and 4 s with le="2.0" and "4.0".
    text = "".join(t["expr"] for t in prometheus_targets())
    assert 'le="2.0"' in text and 'le="4.0"' in text
    buckets = {s for s in served() if s == "gateway_chat_answer_latency_seconds_bucket"}
    assert buckets


def test_panel_ids_are_unique_and_every_panel_has_a_source():
    panels = dashboard()["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    for panel in panels:
        if panel["type"] not in ("row", "text"):
            assert panel["targets"], panel["title"]


def test_dashboard_covers_what_was_asked_for():
    titles = " | ".join(p["title"] for p in dashboard()["panels"])
    for needed in (
        "latency percentiles by model",
        "Requests per second by model",
        "In-flight",
        "burn rate",
        "Jev call latency",
        "Jev est. cost",
        "agreement",
        "Primacy bias",
        "Model health",
        "Time models stayed disabled",
        "auth failures",
    ):
        assert needed.lower() in titles.lower(), needed


def test_sql_panels_read_only_tables_the_role_may_select():
    grants = (ROOT / "grafana/db-setup.sql").read_text()
    allowed = set(
        re.search(r"GRANT SELECT ON ([\w, ]+) TO grafana_ro", grants)[1].split(", ")  # type: ignore[index]
    )
    used = set()
    panels = list(dashboard()["panels"])
    panels.append({"targets": [dashboard()["annotations"]["list"][0]["target"]]})
    for panel in panels:
        for target in panel.get("targets", []):
            if "rawSql" in target:
                used |= set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", target["rawSql"]))
    used -= {"e", "next_at"}  # the CTE and a column inside EXTRACT
    assert used and used <= allowed
    assert "INSERT" not in grants and "UPDATE" not in grants


def test_grafana_is_viewer_only_and_not_published():
    compose = yaml.safe_load((ROOT / "compose.yml").read_text())["services"]
    grafana = compose["grafana"]
    assert "ports" not in grafana
    env = grafana["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert env["GF_AUTH_BASIC_ENABLED"] == "false"
    assert env["GF_SERVER_SERVE_FROM_SUB_PATH"] == "true"
    # A pinned version, not latest.
    assert re.fullmatch(r"grafana/grafana:\d+\.\d+\.\d+", grafana["image"])
    for volume in grafana["volumes"]:
        assert volume.endswith(":ro"), volume


def test_caddy_proxies_grafana_but_never_metrics():
    caddy = (ROOT / "Caddyfile").read_text()
    assert (
        "@grafana path /grafana /grafana/*" in caddy
        and "reverse_proxy grafana:3000" in caddy
    )
    assert "prometheus" not in caddy
    for line in caddy.splitlines():
        if "reverse_proxy" in line or line.strip().startswith("@"):
            assert "/metrics" not in line


def test_datasource_uids_match_the_dashboard():
    sources = yaml.safe_load(
        (ROOT / "grafana/provisioning/datasources/datasources.yml").read_text()
    )["datasources"]
    uids = {s["uid"] for s in sources}
    used = {
        t["datasource"]["uid"]
        for p in dashboard()["panels"]
        for t in p.get("targets", [])
    }
    assert used <= uids
    postgres = next(s for s in sources if s["uid"] == "postgres")
    assert postgres["user"] == "grafana_ro"
    # The password comes from the environment, never from the file.
    assert postgres["secureJsonData"]["password"] == "$GRAFANA_DB_PASSWORD"
