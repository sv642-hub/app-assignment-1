# ruff: noqa: E501
"""Writes the two dashboards Grafana loads, in grafana/dashboards/:
smart-gateway-summary.json (embedded in the app) and smart-gateway.json (the
full one).

The JSON is generated so panels stay short to edit here and the file stays
consistent. Edit this script, run it, and commit both. `--check` fails if the
committed JSON differs (CI runs it through tests/test_grafana.py).

    uv run python scripts/build_grafana_dashboard.py [--check]
"""

import json
import sys
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana/dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "postgres"}
R = "$__rate_interval"
MODEL = 'model=~"$model"'
# Latency and availability targets (docs/MONITORING.md, "Targets").
SLO_AVAILABILITY = 0.99
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
SMALL_TARGET_S, LARGE_TARGET_S = 2, 4

_panels: list[dict] = []
_next_id = 1
_cursor = {"x": 0, "y": 0, "row_h": 0}


def _place(w: int, h: int) -> dict:
    c = _cursor
    if c["x"] + w > 24:
        c["x"], c["y"], c["row_h"] = 0, c["y"] + c["row_h"], 0
    pos = {"x": c["x"], "y": c["y"], "w": w, "h": h}
    c["x"] += w
    c["row_h"] = max(c["row_h"], h)
    return pos


def _add(panel: dict, w: int, h: int) -> None:
    global _next_id
    panel["id"] = _next_id
    _next_id += 1
    panel["gridPos"] = _place(w, h)
    _panels.append(panel)


def _reset() -> None:
    """Start an empty dashboard."""
    global _next_id
    _panels.clear()
    _next_id = 1
    _cursor.update(x=0, y=0, row_h=0)


def row(title: str) -> None:
    c = _cursor
    c["x"], c["y"], c["row_h"] = 0, c["y"] + c["row_h"], 0
    _add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
    c["y"] += 1
    c["row_h"] = 0


def _prom(exprs: list[tuple[str, str]], instant: bool = False) -> list[dict]:
    return [
        {
            "datasource": PROM,
            "refId": chr(65 + i),
            "expr": expr,
            "legendFormat": legend,
            "instant": instant,
            "range": not instant,
        }
        for i, (expr, legend) in enumerate(exprs)
    ]


def _sql(sql: str, fmt: str) -> list[dict]:
    return [
        {
            "datasource": PG,
            "refId": "A",
            "format": fmt,
            "rawQuery": True,
            "editorMode": "code",
            "rawSql": sql,
        }
    ]


def _steps(*pairs: tuple[str, float | None]) -> dict:
    return {
        "mode": "absolute",
        "steps": [{"color": c, "value": v} for c, v in pairs],
    }


def timeseries(
    title, exprs, unit="short", w=8, h=8, stack=False, desc="", minimum=None,
    maximum=None, thresholds=None, bars=False,
):  # fmt: skip
    custom = {
        "lineWidth": 1 if stack or bars else 2,
        "fillOpacity": 25 if stack or bars else 0,
        "showPoints": "never",
        "spanNulls": False,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "drawStyle": "bars" if bars else "line",
    }
    if thresholds:
        custom["thresholdsStyle"] = {"mode": "line"}
    defaults = {"unit": unit, "custom": custom}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    if thresholds:
        defaults["thresholds"] = thresholds
    _add(
        {
            "type": "timeseries",
            "title": title,
            "description": desc,
            "datasource": PROM,
            "targets": _prom(exprs),
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        },
        w,
        h,
    )  # fmt: skip


def stat(title, expr, unit="short", w=4, h=4, desc="", thresholds=None,
         mappings=None, decimals=None):  # fmt: skip
    defaults: dict = {
        "unit": unit,
        "thresholds": thresholds or _steps(("blue", None)),
        "mappings": mappings or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    _add(
        {
            "type": "stat",
            "title": title,
            "description": desc,
            "datasource": PROM,
            "targets": _prom([(expr, "")], instant=True),
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"colorMode": "value", "graphMode": "none",
                        "reduceOptions": {"calcs": ["lastNotNull"]}},
        },
        w,
        h,
    )  # fmt: skip


def prom_table(title, expr, w=8, h=8, desc=""):
    _add(
        {
            "type": "table",
            "title": title,
            "description": desc,
            "datasource": PROM,
            "targets": [{**_prom([(expr, "")], instant=True)[0], "format": "table"}],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showHeader": True},
            "transformations": [
                {"id": "organize",
                 "options": {"excludeByName": {"Time": True, "Value": True,
                                               "__name__": True, "job": True,
                                               "instance": True}}}
            ],
        },
        w,
        h,
    )  # fmt: skip


def sql_table(title, sql, w=12, h=8, desc=""):
    _add(
        {
            "type": "table",
            "title": title,
            "description": desc,
            "datasource": PG,
            "targets": _sql(sql, "table"),
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showHeader": True},
        },
        w,
        h,
    )


def sql_series(title, sql, w=12, h=8, desc="", stack=True, interval=None):
    _add(
        {
            **({"interval": interval} if interval else {}),
            "type": "timeseries",
            "title": title,
            "description": desc,
            "datasource": PG,
            "targets": _sql(sql, "time_series"),
            "fieldConfig": {
                "defaults": {
                    "decimals": 0,
                    "min": 0,
                    "custom": {
                        "drawStyle": "bars",
                        "fillOpacity": 60,
                        "stacking": {"mode": "normal" if stack else "none",
                                     "group": "A"},
                    }
                },
                "overrides": [],
            },
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        },
        w,
        h,
    )  # fmt: skip


def sql_bars(title, sql, w=12, h=8, desc=""):
    _add(
        {
            "type": "barchart",
            "title": title,
            "description": desc,
            "datasource": PG,
            "targets": _sql(sql, "table"),
            "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100},
                            "overrides": []},
            "options": {"xField": "order", "showValue": "always",
                        "legend": {"displayMode": "list"}},
        },
        w,
        h,
    )  # fmt: skip


def model_health(w: int, h: int) -> None:
    _add(
        {
            "type": "state-timeline",
            "title": "Model health (controller)",
            "datasource": PROM,
            "targets": _prom([("gateway_model_disabled", "{{model}}")]),
            "fieldConfig": {
                "defaults": {
                    "mappings": [{"type": "value", "options": {
                        "0": {"text": "serving", "color": "green"},
                        "1": {"text": "disabled", "color": "red"}}}],
                    "custom": {"fillOpacity": 70},
                },
                "overrides": [],
            },
            "options": {"showValue": "never", "mergeValues": True,
                        "legend": {"showLegend": False}},
        },
        w,
        h,
    )  # fmt: skip


def interventions(title: str, where: str = "", desc: str = "") -> None:
    """Counted from the saved interventions, so each event is one whole bar. A
    Prometheus increase() over a short step showed no data. Empty minutes are
    filled with 0, or a lone bar stretches over its neighbours."""
    sql_series(
        title,
        "SELECT $__timeGroupAlias(at, $__interval, 0), "
        "action || ' ' || backend AS metric, count(*) AS value "
        f"FROM interventions WHERE $__timeFilter(at){where} "
        "GROUP BY 1, 2 ORDER BY 1",
        desc=desc,
        interval="1m",
    )  # fmt: skip


def quantiles(metric: str, by: str, selector: str = "") -> list[tuple[str, str]]:
    """p50, p90, p95 and p99 of a histogram, one line each per `by` value."""
    out = []
    for q, name in ((0.5, "p50"), (0.9, "p90"), (0.95, "p95"), (0.99, "p99")):
        inner = f"sum by (le, {by}) (rate({metric}_bucket{selector}[{R}]))"
        out.append((f"histogram_quantile({q}, {inner})", f"{name} {{{{{by}}}}}"))
    return out


def percentiles(metric: str, by: str, selector: str, wanted) -> list[tuple[str, str]]:
    """One line per percentile in `wanted` and `by` value."""
    return [
        pair for pair in quantiles(metric, by, selector)
        if pair[1].split(" ")[0] in wanted
    ]  # fmt: skip


OPEN_ROWS = ("Overview", "Latency")


def _fold(panels: list[dict]) -> list[dict]:
    """Collapse every row except OPEN_ROWS: a collapsed row keeps its panels
    inside it, and Grafana shows them when the row is clicked."""
    out: list[dict] = []
    current: dict | None = None
    for panel in panels:
        if panel["type"] == "row":
            current = panel
            current["collapsed"] = panel["title"] not in OPEN_ROWS
            out.append(panel)
        elif current is not None and current["collapsed"]:
            current["panels"].append(panel)
        else:
            out.append(panel)
    return out


UNAVAILABLE = "sum(increase(gateway_chat_unavailable_total[$__range]))"
ANSWERED = "sum(increase(gateway_chat_requests_total[$__range]))"
BAD_SHARE = f"({UNAVAILABLE} / ({ANSWERED} + {UNAVAILABLE}))"
FORMAT_PASS = (
    f'sum by (model) (rate(gateway_chat_requests_total{{format_check="passed"}}[{R}]))'
    f" / sum by (model) (rate(gateway_chat_requests_total[{R}]))"
)
AGREEMENT = (
    f'sum by (model) (rate(gateway_semantic_samples_total{{sample="agree"}}[{R}]))'
    f" / sum by (model) (rate(gateway_semantic_samples_total"
    f'{{sample=~"agree|disagree"}}[{R}]))'
)


def gateway_stat(w: int) -> None:
    stat("Gateway", 'up{job="gateway"}', w=w,
         mappings=[{"type": "value", "options": {
             "0": {"text": "DOWN", "color": "red"},
             "1": {"text": "UP", "color": "green"}}}])  # fmt: skip


def disabled_stat(w: int) -> None:
    stat("Models disabled", "sum(gateway_model_disabled)", w=w,
         thresholds=_steps(("green", None), ("red", 1)),
         desc="Models the controller has switched off right now.")  # fmt: skip


def traffic_stat(w: int) -> None:
    stat("Requests per second", f"sum(rate(gateway_chat_requests_total[{R}]))",
         unit="reqps", w=w, decimals=2)  # fmt: skip


def answered_stat(w: int) -> None:
    stat("Answered, no 503", f"1 - {BAD_SHARE}", unit="percentunit", w=w,
         decimals=2, thresholds=_steps(("red", None), ("orange", 0.98),
                                       ("green", SLO_AVAILABILITY)),
         desc="Share of requests answered without a 503 in the selected time "
              "range. Target 99%.")  # fmt: skip


def traffic_by_model(w: int, h: int) -> None:
    timeseries("Requests per second by model",
               [(f"sum by (model) (rate(gateway_chat_requests_total[{R}]))",
                 "{{model}}")], unit="reqps", stack=True, minimum=0,
               w=w, h=h)  # fmt: skip


def burn(window: str) -> str:
    """How fast `window` spends the availability error budget."""
    return (
        f"(sum(rate(gateway_chat_unavailable_total[{window}])) / "
        f"(sum(rate(gateway_chat_requests_total[{window}])) + "
        f"sum(rate(gateway_chat_unavailable_total[{window}])))) "
        f"/ {1 - SLO_AVAILABILITY}"
    )


def build_summary() -> dict:
    """Eight panels for the app's Metrics screen: is it up, how fast, which
    model answers, did the controller act, are the replies good."""
    _reset()
    gateway_stat(6)
    disabled_stat(6)
    traffic_stat(6)
    answered_stat(6)
    timeseries(
        "Latency p95 by model",
        percentiles("gateway_chat_answer_latency_seconds", "model", "", ("p95",)),
        unit="s", w=12, h=7, minimum=0,
        desc="Whole gateway time for answered requests.",
    )  # fmt: skip
    traffic_by_model(12, 7)
    model_health(12, 7)
    timeseries(
        "Reply quality by model",
        [(FORMAT_PASS, "format passed {{model}}"),
         (AGREEMENT, "agrees with Jev {{model}}")],
        unit="percentunit", w=12, h=7, minimum=0, maximum=1,
        desc="Share of replies that passed the format check, and share whose "
             "intent matched Jev's. Agreement is not a measure of accuracy.",
    )  # fmt: skip
    return _dashboard(
        "smart-gateway-summary", "Smart AI Gateway summary", list(_panels), []
    )


def build() -> dict:
    # --- Overview ---------------------------------------------------------
    _reset()
    row("Overview")
    gateway_stat(6)
    disabled_stat(6)
    stat("In flight now", "gateway_chat_in_flight", w=6,
         thresholds=_steps(("green", None), ("orange", 5), ("red", 20)),
         desc="Requests being handled right now.")  # fmt: skip
    traffic_stat(6)
    answered_stat(8)
    stat("Error budget left", f"1 - {BAD_SHARE} / {1 - SLO_AVAILABILITY}",
         unit="percentunit", w=8, decimals=0,
         thresholds=_steps(("red", None), ("orange", 0.25), ("green", 0.5)),
         desc="1 means no 503s; 0 means the 1% allowance is used up; below 0 "
              "means the target is missed. Over the selected time range, not "
              "30 days.")  # fmt: skip
    stat("Jev calls", "sum(increase(gateway_jev_answered_calls_total[$__range]))",
         w=8, decimals=0, desc="Answered Jev calls in the time range.")  # fmt: skip

    # --- Golden signals ---------------------------------------------------
    row("Latency")
    timeseries(
        "Request latency by model (p50 and p95)",
        percentiles("gateway_chat_answer_latency_seconds", "model",
                    f"{{{MODEL}}}", ("p50", "p95")),
        unit="s", w=24, minimum=0,
        desc="Whole gateway time for answered requests: Jev, model, checks.",
    )  # fmt: skip

    row("Latency detail")
    timeseries(
        "Request latency percentiles by model",
        quantiles("gateway_chat_answer_latency_seconds", "model",
                  f"{{{MODEL}}}"),
        unit="s", w=12, minimum=0,
        desc="Whole gateway time for answered requests: Jev, model, checks.",
    )  # fmt: skip
    timeseries(
        "Qwen call latency percentiles by model",
        quantiles("gateway_backend_call_latency_seconds", "model",
                  f'{{{MODEL},outcome="ok"}}'),
        unit="s", w=12, minimum=0,
        desc="Only the call to the model, so Jev time is not mixed in.",
    )  # fmt: skip
    timeseries(
        "Request latency percentiles, all requests incl. 503s",
        [(f"histogram_quantile({q}, sum by (le) "
          f"(rate(gateway_chat_latency_seconds_bucket[{R}])))", name)
         for q, name in ((0.5, "p50"), (0.9, "p90"), (0.95, "p95"),
                         (0.99, "p99"))],
        unit="s", w=12, minimum=0,
        desc="Not split by model. 503s are in this histogram.",
    )  # fmt: skip
    timeseries(
        "Request latency percentiles by route",
        quantiles("gateway_chat_answer_latency_seconds", "route"),
        unit="s", w=12, minimum=0,
        desc="standard and complex routes, whichever model answered.",
    )  # fmt: skip

    row("Traffic, errors, saturation")
    traffic_by_model(8, 8)
    timeseries("Requests per second by route",
               [(f"sum by (route) (rate(gateway_chat_requests_total[{R}]))",
                 "{{route}}")], unit="reqps", stack=True, minimum=0)  # fmt: skip
    timeseries("503 rate (no backend could answer)",
               [(f"sum(rate(gateway_chat_unavailable_total[{R}]))", "503s")],
               unit="reqps", minimum=0)  # fmt: skip
    timeseries("Qwen call errors by model and code",
               [(f"sum by (model, error) "
                 f"(rate(gateway_backend_errors_total[{R}]))",
                 "{{model}} {{error}}")], unit="ops", minimum=0)  # fmt: skip
    timeseries("Jev fallbacks by reason",
               [(f"sum by (reason) (rate(gateway_jev_fallbacks_total[{R}]))",
                 "{{reason}}")], unit="ops", stack=True, minimum=0,
               desc="Requests routed by the rule router instead of Jev.")  # fmt: skip
    timeseries("In-flight requests (saturation)",
               [("gateway_chat_in_flight", "in flight"),
                ("max_over_time(gateway_chat_in_flight[1m])", "max, last minute")],
               minimum=0,
               desc="A hung call is invisible in latency histograms until it "
                    "ends. It shows here while it is still waiting.")  # fmt: skip
    timeseries("Slowest failed Qwen calls (p99)",
               [(f"histogram_quantile(0.99, sum by (le, model) (rate("
                 f'gateway_backend_call_latency_seconds_bucket{{outcome="failed"}}'
                 f"[{R}])))", "{{model}}")], unit="s", minimum=0,
               desc="How long failing calls take before they give up.")  # fmt: skip
    timeseries("Qwen calls: attempted vs completed vs failed",
               [(f"sum by (model) (rate(gateway_backend_calls_attempted_total[{R}]))",
                 "attempted {{model}}"),
                (f"sum by (model) (rate(gateway_backend_calls_completed_total[{R}]))",
                 "completed {{model}}"),
                (f"sum by (model) (rate(gateway_backend_calls_failed_total[{R}]))",
                 "failed {{model}}")], unit="ops", minimum=0)  # fmt: skip

    # --- SLO ----------------------------------------------------------------
    row("SLOs and error budget")
    timeseries("Availability burn rate (target 99% answered)",
               [(burn("5m"), "5 minutes"), (burn("1h"), "1 hour"),
                (burn("6h"), "6 hours")], minimum=0,
               thresholds=_steps(("green", None), ("red", 1)),
               desc="1 spends the 1% allowance at exactly the budgeted pace. "
                    "Above 1 the budget runs out early.")  # fmt: skip
    timeseries(
        "p95 latency vs target (small 2 s, large 4 s)",
        [(f"histogram_quantile(0.95, sum by (le) (rate("
          f'gateway_chat_answer_latency_seconds_bucket{{model="{SMALL}"}}[{R}])))',
          f"{SMALL} p95 (target {SMALL_TARGET_S} s)"),
         (f"histogram_quantile(0.95, sum by (le) (rate("
          f'gateway_chat_answer_latency_seconds_bucket{{model="{LARGE}"}}[{R}])))',
          f"{LARGE} p95 (target {LARGE_TARGET_S} s)")],
        unit="s", minimum=0,
        thresholds=_steps(("green", None), ("red", LARGE_TARGET_S)),
        desc="The line is the larger target. The targets are proposals picked "
             "from medians, not measured p95s.",
    )  # fmt: skip
    timeseries(
        "Share of requests under the latency target",
        [(f'sum(rate(gateway_chat_answer_latency_seconds_bucket{{model="{SMALL}",'
          f'le="{SMALL_TARGET_S}.0"}}[{R}])) / '
          f'sum(rate(gateway_chat_answer_latency_seconds_count{{model="{SMALL}"}}'
          f"[{R}]))", f"{SMALL} under {SMALL_TARGET_S} s"),
         (f'sum(rate(gateway_chat_answer_latency_seconds_bucket{{model="{LARGE}",'
          f'le="{LARGE_TARGET_S}.0"}}[{R}])) / '
          f'sum(rate(gateway_chat_answer_latency_seconds_count{{model="{LARGE}"}}'
          f"[{R}]))", f"{LARGE} under {LARGE_TARGET_S} s")],
        unit="percentunit", minimum=0, maximum=1,
    )  # fmt: skip
    # --- Quality ------------------------------------------------------------
    row("Reply quality")
    timeseries("Format check pass rate by model", [(FORMAT_PASS, "{{model}}")],
               unit="percentunit", minimum=0, maximum=1)  # fmt: skip
    timeseries("Qwen vs Jev intent agreement by model", [(AGREEMENT, "{{model}}")],
               unit="percentunit", minimum=0, maximum=1,
               desc="The model's intent label against Jev's intent answer for "
                    "the same message. There is one Jev call per request, made "
                    "before the model; Jev does not score the model's reply.")  # fmt: skip
    timeseries("Semantic window: disagreement and coverage",
               [("gateway_semantic_disagreement_rate", "disagreement {{model}}"),
                ("gateway_semantic_coverage", "coverage {{model}}")],
               unit="percentunit", minimum=0, maximum=1,
               desc="From the semantic monitor window, as the controller sees it.")  # fmt: skip
    timeseries("Semantic samples: agree, disagree, no signal",
               [(f"sum by (sample) (rate(gateway_semantic_samples_total[{R}]))",
                 "{{sample}}")], unit="ops", stack=True, minimum=0)  # fmt: skip
    timeseries("Replies by source",
               [(f"sum by (reply_source) (rate(gateway_chat_requests_total[{R}]))",
                 "{{reply_source}}")], unit="reqps", stack=True, minimum=0,
               desc="format_fallback means the model's reply failed the check.")  # fmt: skip
    timeseries("Replies repaired by the gateway",
               [(f"sum by (model) (rate(gateway_uncarded_facts_dropped_total[{R}]))",
                 "facts dropped {{model}}"),
                (f"sum by (model) (rate(gateway_empty_unknown_filled_total[{R}]))",
                 "empty unknown filled {{model}}"),
                (f"sum by (model) (rate(gateway_semantic_not_compared_total[{R}]))",
                 "not compared {{model}}")], unit="ops", minimum=0)  # fmt: skip

    # --- Self healing -------------------------------------------------------
    row("Self-healing and releases")
    model_health(12, 6)
    timeseries("Monitor failure rate and window size",
               [("gateway_monitor_failure_rate", "failure rate {{model}}"),
                ("gateway_monitor_window_samples", "samples {{model}}")],
               w=12, h=6, minimum=0)  # fmt: skip
    interventions(
        "Controller interventions",
        desc="Disabled, re-enabled, rolled back and operator actions. "
             "The same events are marked on every graph.",
    )  # fmt: skip
    stat("Rollbacks", 'sum(increase(gateway_controller_interventions_total{'
         'action="rolled_back"}[$__range]))', w=3, h=7, decimals=0,
         thresholds=_steps(("green", None), ("orange", 1)))  # fmt: skip
    prom_table("Release now (per model)", "gateway_model_release == 1",
               w=9, h=7, desc="Version each model serves and whether the "
                              "release is stable, watching or rolled back.")  # fmt: skip
    sql_table(
        "Time models stayed disabled",
        "WITH e AS (SELECT at, backend, action, "
        "lead(at) OVER (PARTITION BY backend ORDER BY at) AS next_at, "
        "lead(action) OVER (PARTITION BY backend ORDER BY at) AS next_action "
        "FROM interventions) "
        "SELECT at AS time, backend, "
        "round(extract(epoch FROM next_at - at)::numeric, 1) AS seconds_disabled, "
        "action AS disabled_by, next_action AS ended_by "
        "FROM e WHERE action IN ('disabled', 'operator_disabled') "
        "AND next_at IS NOT NULL AND $__timeFilter(at) ORDER BY at DESC LIMIT 100",
        desc="From the saved interventions: how long each disable lasted "
             "until the model was re-enabled or the next action.",
    )  # fmt: skip
    sql_table(
        "Release history",
        "SELECT at AS time, backend, action, from_version, version, actor, reason "
        "FROM model_releases WHERE $__timeFilter(at) ORDER BY at DESC LIMIT 100",
        desc="Deploys, confirmations and rollbacks.",
    )  # fmt: skip

    # --- Jev ----------------------------------------------------------------
    row("Jev: latency, cost and decisions")
    timeseries(
        "Jev call latency percentiles",
        [(f'histogram_quantile({q}, sum by (le) (rate('
          f'gateway_jev_call_latency_seconds_bucket{{outcome="ok"}}[{R}])))', name)
         for q, name in ((0.5, "p50"), (0.9, "p90"), (0.95, "p95"),
                         (0.99, "p99"))],
        unit="s", minimum=0, desc="Answered Jev calls. The timeout is 2 s by default.",
    )  # fmt: skip
    timeseries("Jev calls by outcome",
               [(f"sum by (outcome) (rate(gateway_jev_call_latency_seconds_count"
                 f"[{R}]))", "{{outcome}}")], unit="ops", stack=True, minimum=0,
               desc="ok, timeout or error. Calls skipped by the open circuit "
                    "breaker are in Jev fallbacks.")  # fmt: skip
    timeseries("Jev tokens per second",
               [(f"sum by (direction) (rate(gateway_jev_tokens_total[{R}]))",
                 "{{direction}}")], unit="short", stack=True, minimum=0,
               desc="Mock Jev reports 0 tokens.")  # fmt: skip
    stat("Jev tokens (range)",
         "sum(increase(gateway_jev_tokens_total[$__range]))", w=4, h=4, decimals=0)  # fmt: skip
    stat("Jev est. cost (range)",
         "sum(increase(gateway_jev_estimated_cost_usd_total[$__range]))",
         unit="currencyUSD", w=4, h=4, decimals=4,
         desc="Empty while APP_JEV_INPUT_PRICE_PER_MILLION_USD is unset.")  # fmt: skip
    sql_series(
        "Jev route choice over time",
        "SELECT $__timeGroupAlias(finished_at, $__interval), choice AS metric, "
        "count(*) AS value FROM jev_assessments WHERE $__timeFilter(finished_at) "
        "AND question = 'route' AND status = 'ok' GROUP BY 1, 2 ORDER BY 1",
        desc="Which route Jev picked for each request.",
    )  # fmt: skip
    sql_bars(
        "Primacy bias: share of 'complex' by option order",
        "SELECT option_order[1] || '-first' AS \"order\", "
        "round(100.0 * count(*) FILTER (WHERE choice = 'complex') / count(*), 1) "
        "AS pct_complex FROM jev_assessments WHERE $__timeFilter(finished_at) "
        "AND question = 'route' AND status = 'ok' AND option_order IS NOT NULL "
        "GROUP BY 1 ORDER BY 1",
        desc="Jev's route choice split by which option was listed first. Live "
             "traffic uses one fixed order today (standard first), so only one "
             "bar appears until orders vary. If the two bars differ on the "
             "same messages, that is primacy bias.",
    )  # fmt: skip
    sql_table(
        "Primacy bias: route choices by option order",
        "SELECT option_order[1] || '-first' AS \"order\", choice, count(*) AS calls "
        "FROM jev_assessments WHERE $__timeFilter(finished_at) AND "
        "question = 'route' AND status = 'ok' AND option_order IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1, 2", w=12,
    )  # fmt: skip
    # --- Security -----------------------------------------------------------
    row("Security")
    timeseries("Operator route auth failures",
               [(f"sum by (reason) "
                 f"(increase(gateway_operator_auth_failures_total[{R}]))",
                 "{{reason}}")], bars=True, stack=True, minimum=0, w=12,
               desc="401 and 403 on /v1/operator and /v1/faults. Normally flat zero.")  # fmt: skip
    interventions("Operator actions", " AND action LIKE 'operator%'")

    return _dashboard(
        "smart-gateway", "Smart AI Gateway", _fold(_panels), [model_variable()]
    )


def model_variable() -> dict:
    return {
        "name": "model",
        "label": "Model",
        "type": "query",
        "datasource": PROM,
        "query": {
            "query": "label_values(gateway_chat_requests_total, model)",
            "refId": "model",
        },
        "definition": "label_values(gateway_chat_requests_total, model)",
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "allValue": ".+",
        "current": {"text": "All", "value": "$__all"},
    }


def _dashboard(uid: str, title: str, panels: list[dict], variables: list) -> dict:
    return {
        "uid": uid,
        "title": title,
        "tags": ["smart-gateway"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-15m", "to": "now"},
        "graphTooltip": 1,
        "templating": {"list": variables},
        "annotations": {
            "list": [
                {
                    "name": "Controller interventions",
                    "enable": True,
                    "iconColor": "red",
                    "datasource": PG,
                    "target": {
                        "datasource": PG,
                        "refId": "Anno",
                        "format": "table",
                        "rawQuery": True,
                        "editorMode": "code",
                        "rawSql": "SELECT at AS time, backend || ': ' || action || "
                        "' (' || reason || ')' AS text, action AS tags "
                        "FROM interventions WHERE $__timeFilter(at) ORDER BY at",
                    },
                }
            ]
        },
        "panels": panels,
    }


def main() -> int:
    for name, make in (
        ("smart-gateway.json", build),
        ("smart-gateway-summary.json", build_summary),
    ):
        out = DASHBOARDS / name
        text_out = json.dumps(make(), indent=2) + "\n"
        if "--check" not in sys.argv:
            out.write_text(text_out)
            print(f"wrote {out}")
        elif not out.exists() or out.read_text() != text_out:
            print(f"{out} is out of date; run scripts/build_grafana_dashboard.py")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
