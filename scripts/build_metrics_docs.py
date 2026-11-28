"""Writes docs/METRICS.md from config/metrics.yaml, the metrics catalog.

Edit the YAML, run this and commit both. `--check` fails if the committed
page differs (CI runs it through tests/test_metrics_catalog.py).

    uv run python scripts/build_metrics_docs.py [--check]
"""

import sys
from pathlib import Path

from app_assignment_1.metrics_catalog import Catalog, load_catalog

OUT = Path(__file__).resolve().parents[1] / "docs/METRICS.md"
STATUS_TEXT = {
    "served": "served by the gateway",
    "derived": "derived in PromQL",
    "offline": "offline: measured by a script, not scraped",
}


def _value(target) -> str:
    value = target.value
    if isinstance(value, dict):
        return ", ".join(f"{size} {number:g}" for size, number in value.items())
    return f"{value:g}"


def _promql(catalog: Catalog, name: str) -> str:
    metric = catalog.metrics[name]
    if metric.per_model:
        return catalog.render(name, "5m", model="small")
    return catalog.render(name, "5m")


def build(catalog: Catalog) -> str:
    lines = [
        "# Metric definitions",
        "",
        "Generated from [`config/metrics.yaml`](../config/metrics.yaml), the "
        "metrics catalog. Edit the YAML and run "
        "`uv run python scripts/build_metrics_docs.py`; a test fails if this "
        "page is stale. The catalog holds what each metric means and the "
        "numbers built on top of the Prometheus series (which are listed in "
        "the [README](../README.md#metrics-and-prometheus)). Settings that differ per "
        "deployment stay `APP_` settings in `config.py`; an entry only names "
        "them. `tests/test_metrics_catalog.py` fails when the dashboard, the "
        "alerts or the docs stop matching it.",
        "",
        "Target status: *proposal* was picked from medians and not measured, "
        "*starting point* is a prototype alert threshold, *goal* is a stated "
        "aim. None of these is a measured result.",
        "",
        "## Targets",
        "",
        "| Target | Value | Status | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    for name, target in catalog.targets.items():
        meaning = " ".join(target.meaning.split())
        lines.append(f"| `{name}` | {_value(target)} | {target.status} | {meaning} |")
    lines += ["", "## Metrics", ""]
    for name, metric in catalog.metrics.items():
        lines += [f"### `{name}`", "", " ".join(metric.description.split()), ""]
        lines.append(f"- **Unit:** {metric.unit}; **kind:** {metric.kind}")
        lines.append(f"- **Status:** {STATUS_TEXT[metric.status]}")
        if metric.target:
            word = "at most" if metric.direction == "max" else "at least"
            lines.append(f"- **Target:** {word} `{metric.target}`")
        if metric.config:
            lines.append(
                "- **Related settings:** "
                + ", ".join(f"`APP_{c.upper()}`" for c in metric.config)
            )
        if metric.used_by:
            lines.append("- **Used by:** " + "; ".join(metric.used_by))
        if metric.source:
            lines.append(f"- **Measured by:** `{metric.source}`")
        else:
            note = " (small model shown)" if metric.per_model else ""
            lines += [
                f"- **PromQL** over 5 minutes{note}:",
                "",
                "  ```promql",
                f"  {_promql(catalog, name)}",
                "  ```",
            ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    text = build(load_catalog())
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT} is out of date; run scripts/build_metrics_docs.py")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
