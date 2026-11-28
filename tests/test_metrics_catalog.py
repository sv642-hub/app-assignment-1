"""The metrics catalog (config/metrics.yaml) and what repeats it.

The catalog defines every metric's meaning and the numbers around it once.
These tests fail when the dashboard, the alerts, the settings defaults or the
docs stop agreeing with it, so it cannot become one more copy that drifts.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.metrics_catalog import Catalog, load_catalog

ROOT = Path(__file__).resolve().parents[1]
SERIES = re.compile(r"\b(gateway_[a-z_]+)\b(?:\{([^}]*)\})?")
MATCHER = re.compile(r"(\w+)\s*(?:=~|!~|!=|=)")
GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
THRESHOLD = re.compile(r"\s*(>=|>)\s*([\d.]+)\s*$")
SIZES: tuple = ("small", "large")

CATALOG = load_catalog()


def alerts() -> dict[str, dict]:
    rules = yaml.safe_load((ROOT / "prometheus/alerts.yml").read_text())
    return {rule["alert"]: rule for rule in rules["groups"][0]["rules"]}


def builder():
    spec = importlib.util.spec_from_file_location(
        "build_grafana_dashboard", ROOT / "scripts/build_grafana_dashboard.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dashboard_titles() -> set[str]:
    data = json.loads((ROOT / "grafana/dashboards/smart-gateway.json").read_text())
    titles = set()
    for panel in data["panels"]:
        titles.add(panel["title"])
        titles.update(inner["title"] for inner in panel.get("panels", []))
    return titles


def metrics_text() -> str:
    settings = Settings(_env_file=None, jev_input_price_per_million_usd=1)
    with TestClient(create_app(settings)) as client:
        client.post("/v1/chat", json={"message": "my card has not arrived"})
        return client.get("/metrics").text


def served() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for family in text_string_to_metric_families(metrics_text()):
        for sample in family.samples:
            out.setdefault(sample.name, set()).update(sample.labels)
    return out


def renders(catalog: Catalog = CATALOG):
    """Every PromQL form of every metric that has one: both functions, each model."""
    for name, metric in catalog.metrics.items():
        if metric.promql is None:
            continue
        for model in SIZES if metric.per_model else (None,):
            for fn in ("rate", "increase"):
                yield name, catalog.render(name, "5m", fn, model)  # type: ignore[arg-type]


def normal(expr: str) -> str:
    """An expression with the zero-fill, spaces and parentheses removed, to
    compare a rule with the catalog's formula, which may spell them differently."""
    expr = THRESHOLD.sub("", expr)
    return re.sub(r"[\s()]|or vector\(0\)", "", expr.replace("or vector(0)", ""))


def threshold(expr: str) -> float:
    match = THRESHOLD.search(expr)
    assert match, expr
    return float(match.group(2))


# --- the catalog itself -----------------------------------------------------


def test_the_catalog_has_the_metrics_the_project_asked_for():
    needed = {
        "chat_availability",
        "chat_unavailable_ratio",
        "answer_latency_p95_seconds",
        "request_latency_p95_seconds",
        "monitor_failure_rate",
        "format_check_pass_ratio",
        "semantic_disagreement_rate",
        "jev_skipped_ratio",
        "escalation_rate",
        "large_model_share",
        "jev_cost_per_request_usd",
        "time_to_disable_seconds",
    }
    assert needed <= set(CATALOG.metrics)
    assert CATALOG.metrics["time_to_disable_seconds"].status == "offline"


def test_a_malformed_catalog_is_refused(tmp_path):
    good = yaml.safe_load((ROOT / "config/metrics.yaml").read_text())
    bad_cases = []
    for edit in (
        lambda d: d["metrics"]["escalation_rate"].update(status="offline"),
        lambda d: d["metrics"]["escalation_rate"].update(target="no_such_target"),
        lambda d: d["metrics"]["escalation_rate"].update(promql="<<no_such>> + 1"),
        lambda d: d["metrics"]["escalation_rate"].update(used_by=["somewhere"]),
        lambda d: d["metrics"]["escalation_rate"].update(typo=1),
        lambda d: d["metrics"]["escalation_rate"].update(direction="max"),
        lambda d: d["metrics"]["escalation_rate"].update(
            promql='x{model="$for_model"}'
        ),
    ):
        data = yaml.safe_load(yaml.safe_dump(good))
        edit(data)
        bad_cases.append(data)
    for data in bad_cases:
        path = tmp_path / "metrics.yaml"
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError):
            load_catalog(path)


def test_rendering_fills_every_placeholder_and_checks_the_model():
    assert "$" not in " ".join(text for _, text in renders())
    assert 'le="2.0"' in CATALOG.render(
        "answer_latency_within_target", "5m", model="small"
    )
    assert 'le="4.0"' in CATALOG.render(
        "answer_latency_within_target", "5m", model="large"
    )
    assert CATALOG.render("error_budget_burn_rate").endswith("/ 0.01")
    with pytest.raises(ValueError):
        CATALOG.render("answer_latency_p95_seconds")  # needs a model
    with pytest.raises(ValueError):
        CATALOG.render("time_to_disable_seconds")  # offline


# --- the series ---------------------------------------------------------------


def test_every_formula_uses_only_series_and_labels_the_gateway_serves():
    series = served()
    problems = []
    for name, expr in renders():
        labels: set[str] = set()
        for metric, matchers in SERIES.findall(expr):
            if metric not in series:
                problems.append(f"{name}: no series {metric}")
                continue
            labels |= series[metric]
            for label in MATCHER.findall(matchers):
                if label not in series[metric]:
                    problems.append(f"{name}: {metric} has no label {label}")
        for group in GROUPING.findall(expr):
            for label in filter(None, (x.strip() for x in group.split(","))):
                if label not in labels:
                    problems.append(f"{name}: no label {label}")
    assert problems == []


def test_the_formulas_read_the_label_values_the_gateway_uses():
    # A label that exists but never takes the value (route="complx") would
    # pass the check above and always read zero.
    text = metrics_text()
    routes = set(
        re.findall(
            r'gateway_chat_answer_latency_seconds_count\{[^}]*route="(\w+)"', text
        )
    )
    for _, expr in renders():
        for route in re.findall(r'route="(\w+)"', expr):
            assert route in routes, route
        for model in re.findall(r'model="([\w.\-]+)"', expr):
            assert model in set(CATALOG.models.values()), model


def test_the_two_models_are_the_ones_in_backends_json():
    backends = json.loads(Settings(_env_file=None).backends_path.read_text())
    by_route = {b["route"]: b["name"] for b in backends}
    assert by_route == {
        "standard": CATALOG.models["small"],
        "complex": CATALOG.models["large"],
    }


def test_the_two_p95_latencies_are_named_apart_and_read_different_histograms():
    answer = CATALOG.render("answer_latency_p95_seconds", model="small")
    request = CATALOG.render("request_latency_p95_seconds")
    assert "gateway_chat_answer_latency_seconds_bucket" in answer
    assert "gateway_chat_latency_seconds_bucket" in request
    assert CATALOG.metrics["answer_latency_p95_seconds"].target != (
        CATALOG.metrics["request_latency_p95_seconds"].target
    )


def test_each_latency_target_is_a_bucket_edge_of_its_histogram():
    # Percentiles interpolate inside a bucket, and "share under the target"
    # counts a bucket exactly, so a target between edges would be approximate.
    text = metrics_text()

    def edges(name: str) -> set[float]:
        found = re.findall(rf'{name}_bucket\{{[^}}]*le="([\d.]+)"', text)
        return {float(x) for x in found} | {
            float(x) for x in re.findall(rf'{name}_bucket\{{le="([\d.]+)"', text)
        }

    answer = CATALOG.targets["answer_latency_p95_seconds"].value
    assert isinstance(answer, dict)
    assert set(answer.values()) <= edges("gateway_chat_answer_latency_seconds")
    request = CATALOG.target_value("request_latency_p95_alert_seconds")
    assert request in edges("gateway_chat_latency_seconds")


# --- the dashboard ------------------------------------------------------------


def test_targets_match_the_dashboard_constants():
    module = builder()
    assert module.SLO_AVAILABILITY == CATALOG.target_value("availability")
    assert module.SMALL_TARGET_S == CATALOG.target_value(
        "answer_latency_p95_seconds", "small"
    )
    assert module.LARGE_TARGET_S == CATALOG.target_value(
        "answer_latency_p95_seconds", "large"
    )
    assert (module.SMALL, module.LARGE) == (
        CATALOG.models["small"],
        CATALOG.models["large"],
    )


def test_every_panel_a_metric_names_exists_on_the_dashboard():
    titles = dashboard_titles()
    for name, metric in CATALOG.metrics.items():
        for ref in metric.used_by:
            kind, _, what = ref.partition(":")
            if kind == "grafana":
                assert what in titles, f"{name}: no panel {what!r}"


def dashboard_exprs() -> dict[str, str]:
    data = json.loads((ROOT / "grafana/dashboards/smart-gateway.json").read_text())
    out: dict[str, str] = {}
    for panel in data["panels"] + [
        inner for outer in data["panels"] for inner in outer.get("panels", [])
    ]:
        out[panel["title"]] = " ".join(
            t.get("expr", "") for t in panel.get("targets", [])
        )
    return out


def test_a_panel_a_metric_names_draws_the_catalogs_formula():
    # The dashboard is generated from the catalog, so a panel listed under a
    # metric must contain that metric's PromQL for some window and function.
    exprs = dashboard_exprs()
    windows = ("$__rate_interval", "$__range", "$__interval", "5m", "1h", "6h")
    for name, metric in CATALOG.metrics.items():
        wanted = [
            r.partition(":")[2] for r in metric.used_by if r.startswith("grafana:")
        ]
        if not wanted or metric.promql is None:
            continue
        forms = {
            CATALOG.render(name, window, fn, model)  # type: ignore[arg-type]
            for window in windows
            for fn in ("rate", "increase")
            for model in (SIZES if metric.per_model else (None,))
        }
        for title in wanted:
            assert any(form in exprs[title] for form in forms), f"{title!r}: not {name}"


def test_the_new_panels_exist():
    titles = dashboard_titles()
    assert "Escalation rate and large-model share" in titles
    assert "Jev est. cost per request (range)" in titles
    assert "Jev skipped share (rule router used)" in titles


# --- the alerts ---------------------------------------------------------------


def test_every_alert_a_metric_names_exists_and_uses_its_formula_and_target():
    rules = alerts()
    seen = set()
    for name, metric in CATALOG.metrics.items():
        for ref in metric.used_by:
            kind, _, what = ref.partition(":")
            if kind != "alert":
                continue
            assert what in rules, f"{name}: no alert {what}"
            seen.add(what)
            expr = rules[what]["expr"]
            if metric.promql and "histogram_quantile" in metric.promql:
                formula = CATALOG.render(name, "5m")
            elif metric.status == "derived":
                formula = CATALOG.render(name, "5m")
            else:
                formula = None
            if formula:
                assert normal(expr) == normal(formula), f"{what} differs from {name}"
            if metric.target and metric.status == "derived":
                assert threshold(expr) == CATALOG.target_value(metric.target), what
    # The alerts with a ratio or percentile threshold are all in the catalog.
    assert seen == {
        "ChatMostlyFailing",
        "JevMostlySkipped",
        "ChatLatencyHigh",
        "ModelFailureRateRising",
    }


def test_the_early_warning_alert_matches_the_catalog_and_the_controller():
    expr = alerts()["ModelFailureRateRising"]["expr"]
    settings = Settings(_env_file=None)
    early = CATALOG.target_value("model_failure_early_warning")
    assert f"gateway_monitor_failure_rate >= {early:g}" in expr
    # The warning must come before the controller acts, or it is useless.
    assert 0 < early < settings.monitor_failure_threshold
    # The alert's sample count is typed into the rule; keep it at the default.
    minimum = CATALOG.target_value("model_failure_min_samples")
    assert f"gateway_monitor_window_samples >= {minimum:g}" in expr
    assert minimum == settings.monitor_min_samples


def test_the_alert_thresholds_in_the_catalog_are_the_ones_in_the_rules():
    rules = alerts()
    assert threshold(rules["ChatMostlyFailing"]["expr"]) == CATALOG.target_value(
        "chat_unavailable_alert"
    )
    assert threshold(rules["JevMostlySkipped"]["expr"]) == CATALOG.target_value(
        "jev_skipped_alert"
    )
    assert threshold(rules["ChatLatencyHigh"]["expr"]) == CATALOG.target_value(
        "request_latency_p95_alert_seconds"
    )


# --- settings -----------------------------------------------------------------


def test_metrics_name_settings_that_exist_and_copy_none_of_them():
    fields = Settings.model_fields
    for name, metric in CATALOG.metrics.items():
        for setting in metric.config:
            assert setting in fields, f"{name}: no setting {setting}"
    # The catalog never holds a controller setting as a target.
    assert not {"monitor_failure_threshold", "semantic_alert_margin"} & set(
        CATALOG.targets
    )


def test_the_catalog_is_not_read_by_the_gateway():
    source = (ROOT / "src/app_assignment_1/config.py").read_text()
    assert "metrics_catalog" not in source and "metrics.yaml" not in source


# --- offline metrics and files ------------------------------------------------


def test_offline_metrics_name_a_script_that_measures_them():
    offline = [m for m in CATALOG.metrics.values() if m.status == "offline"]
    assert offline
    for metric in offline:
        path, _, symbol = (metric.source or "").partition("#")
        text = (ROOT / path).read_text()
        assert symbol in text, metric.source


def test_files_a_metric_names_exist():
    for name, metric in CATALOG.metrics.items():
        for ref in metric.used_by:
            kind, _, what = ref.partition(":")
            if kind in ("docs", "script"):
                assert (ROOT / what).exists(), f"{name}: {what}"


# --- the docs -----------------------------------------------------------------


def test_the_metrics_page_matches_the_catalog():
    result = subprocess.run(
        [sys.executable, "scripts/build_metrics_docs.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def _pct(value: float) -> str:
    return f"{value * 100:g}%"


def test_the_docs_state_the_catalog_numbers():
    readme = (ROOT / "README.md").read_text()
    runbook = (ROOT / "docs/MONITORING.md").read_text()
    small = CATALOG.target_value("answer_latency_p95_seconds", "small")
    large = CATALOG.target_value("answer_latency_p95_seconds", "large")
    early = CATALOG.target_value("model_failure_early_warning")
    request = CATALOG.target_value("request_latency_p95_alert_seconds")
    minimum = CATALOG.target_value("model_failure_min_samples")
    for name, text in (("README.md", readme), ("docs/MONITORING.md", runbook)):
        assert f"small under {small:g} s, large under {large:g} s" in text, name
        assert f"{_pct(CATALOG.target_value('availability'))} answered" in text, name
        assert (
            f"{_pct(early)} or more of the calls in its window, "
            f"with at least {minimum:g} calls" in text
        ), name
        assert f"above {request:g} s" in text, name
        assert (
            f"{_pct(CATALOG.target_value('chat_unavailable_alert'))} of chat requests"
            in text
        ), name
    assert "metrics catalog" in runbook and "METRICS.md" in runbook
    assert "METRICS.md" in readme


def test_proposals_are_called_proposals_in_the_docs():
    # The latency and availability targets were picked, not measured. Each doc
    # that states them says so, as the catalog does.
    assert CATALOG.targets["answer_latency_p95_seconds"].status == "proposal"
    assert CATALOG.targets["availability"].status == "proposal"
    for path in ("README.md", "docs/MONITORING.md"):
        text = (ROOT / path).read_text().lower()
        assert "proposal" in text, path


def test_the_layout_list_names_the_catalog_files():
    agents = (ROOT / "AGENTS.md").read_text()
    for needed in (
        "config/metrics.yaml",
        "metrics_catalog.py",
        "docs/METRICS.md",
        "build_metrics_docs.py",
    ):
        assert needed in agents, needed
