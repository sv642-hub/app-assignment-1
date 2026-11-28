"""The metrics catalog: what each metric means, how to compute it and what it
should be, in one file (config/metrics.yaml).

It is not the list of Prometheus series: `metrics.py` is. The catalog holds
the definitions and numbers built on top of the series (ratios, percentiles,
targets). Dashboards, alerts and docs repeat those, and tests/test_metrics_
catalog.py checks they have not drifted. `APP_` settings stay in `config.py`
(they differ per deployment); a catalog entry names the ones it relates to
and never copies their values.

The gateway does not import this at run time. It needs PyYAML, which the
`dev` extra installs.

PromQL in the catalog may use these placeholders (string.Template syntax):
`$fn` (`rate` or `increase`), `$window`, `$for_model` (a model name),
`$small_model`, `$large_model`, `$target_<name>` for every target (a target
with a small and a large value also gets `$target_<name>_small` and
`_large`), `$target_for_model` and `$target_for_model_le` (the metric's own
target for `$for_model`, plain and as a histogram `le` label), and
`$error_budget`. `<<name>>` inserts another entry's PromQL in parentheses.
"""

import re
from pathlib import Path
from string import Template
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "metrics.yaml"

Size = Literal["small", "large"]
Kind = Literal["ratio", "rate", "latency", "gauge", "cost", "duration"]
Status = Literal["served", "derived", "offline"]
TargetStatus = Literal["proposal", "starting_point", "goal"]
USED_BY = re.compile(r"^(alert|grafana|docs|script):(.+)$")
REF = re.compile(r"<<(\w+)>>")


class Target(BaseModel):
    """A named target value and the unit it is in."""

    model_config = ConfigDict(extra="forbid")

    value: float | dict[Size, float]
    status: TargetStatus
    meaning: str


class MetricDef(BaseModel):
    """One metric's definition, with its PromQL and targets."""

    model_config = ConfigDict(extra="forbid")

    description: str
    unit: str
    kind: Kind
    status: Status
    promql: str | None = None
    # A served or derived metric is computed here; an offline one has a source.
    source: str | None = None
    target: str | None = None
    direction: Literal["min", "max"] | None = None
    per_model: bool = False
    config: list[str] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "MetricDef":
        """Check the definition has what its kind needs."""
        if self.status == "offline":
            if self.promql or not self.source:
                raise ValueError("an offline metric has a source and no promql")
        elif not self.promql or self.source:
            raise ValueError("a served or derived metric has promql and no source")
        if bool(self.target) != bool(self.direction):
            raise ValueError("target and direction go together")
        for ref in self.used_by:
            if not USED_BY.match(ref):
                raise ValueError(
                    f"used_by {ref!r}: use alert:, grafana:, docs: or script:"
                )
        return self


class Catalog(BaseModel):
    """The whole metrics catalog: targets and metric definitions."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    models: dict[Size, str]
    targets: dict[str, Target]
    metrics: dict[str, MetricDef]

    @model_validator(mode="after")
    def _check(self) -> "Catalog":
        """Check that every name the catalog uses is defined."""
        for name, metric in self.metrics.items():
            if metric.target and metric.target not in self.targets:
                raise ValueError(f"{name}: no target {metric.target}")
            for ref in REF.findall(metric.promql or ""):
                if ref not in self.metrics:
                    raise ValueError(f"{name}: no metric {ref} to insert")
            if metric.promql and "$for_model" in metric.promql:
                if not metric.per_model:
                    raise ValueError(f"{name}: uses $for_model but is not per_model")
        return self

    # --- rendering -----------------------------------------------------

    def _values(self, model: Size | None, metric: MetricDef) -> dict[str, str]:
        """Return the values to fill into a metric's PromQL for one model size."""
        values = {
            "small_model": self.models["small"],
            "large_model": self.models["large"],
            "error_budget": _num(1 - self.target_value("availability")),
        }
        for name, target in self.targets.items():
            if isinstance(target.value, dict):
                for size, per_size in target.value.items():
                    values[f"target_{name}_{size}"] = _num(per_size)
            else:
                values[f"target_{name}"] = _num(target.value)
        if model is not None:
            values["for_model"] = self.models[model]
            if metric.target:
                own = self.targets[metric.target].value
                if isinstance(own, dict):
                    values["target_for_model"] = _num(own[model])
                    values["target_for_model_le"] = repr(float(own[model]))
        return values

    def target_value(self, name: str, size: Size | None = None) -> float:
        """Return a target's number, for one model size when it has per-size values."""
        value = self.targets[name].value
        if isinstance(value, dict):
            if size is None:
                raise ValueError(f"{name} has a value per model; give a size")
            return value[size]
        return value

    def render(
        self,
        name: str,
        window: str = "5m",
        fn: Literal["rate", "increase"] = "rate",
        model: Size | None = None,
    ) -> str:
        """The PromQL of one metric. `model` is required for per-model ones."""
        metric = self.metrics[name]
        if metric.promql is None:
            raise ValueError(f"{name} is offline: it has no PromQL")
        if metric.per_model and model is None:
            raise ValueError(f"{name} needs model='small' or 'large'")
        text = metric.promql
        for ref in REF.findall(text):
            inner = self.render(ref, window, fn, model)
            text = text.replace(f"<<{ref}>>", f"({inner})")
        values = self._values(model, metric)
        values.update(fn=fn, window=window)
        return " ".join(Template(text).substitute(values).split())


def _num(value: float) -> str:
    """Format a number without a trailing `.0`."""
    return format(round(value, 10), "g")


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    """Read and validate the catalog YAML."""
    data: Any = yaml.safe_load(path.read_text())
    return Catalog.model_validate(data)
