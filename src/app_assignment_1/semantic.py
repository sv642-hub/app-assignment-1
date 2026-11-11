"""Semantic monitor: does each model's intent agree with Jev's?

The format check catches replies that are malformed. It cannot catch a model
that returns a valid reply for the wrong intent. For that, each request with a
passed format check is scored against Jev's answer to the intent question:

| Jev's intent assessment | Sample |
|---|---|
| `ok`, same label as the model | agree |
| `ok`, different label | disagree |
| `timeout`, `error`, `skipped` or missing | no_signal (lowers coverage) |

Failed format checks and failed calls give no sample here; the failure monitor
in monitor.py counts them. A model serving the other route's traffic sees
different messages, so those samples are kept apart from its own window.

The windows live in the gateway process and reset on restart, like the failure
monitor's. The rules follow the scoring table drafted in PR #45. The baseline,
margin and coverage minimum are placeholders until they are set from dev data.
"""

from collections import deque
from collections.abc import Iterable, Sequence
from typing import Literal

from app_assignment_1.config import Settings
from app_assignment_1.records import JevAssessment
from app_assignment_1.schemas import BackendName, SemanticRules, SemanticStatus

Sample = Literal["agree", "disagree", "no_signal"]
SAMPLES: tuple[Sample, ...] = ("agree", "disagree", "no_signal")
# Baseline plus margin is a sum of floats: 0.30 + 0.15 is 0.44999999999999996.
# Without this, a rate exactly at the limit would be flagged.
THRESHOLD_TOLERANCE = 1e-9


def score(model_intent: str, jev: Sequence[JevAssessment]) -> Sample:
    """Score one model reply that passed the format check against Jev."""
    answer = next((r for r in jev if r.question == "intent"), None)
    if answer is None or answer.status != "ok":
        return "no_signal"
    return "agree" if answer.choice == model_intent else "disagree"


class SemanticMonitor:
    """Sliding windows of agreement with Jev, one per model.

    It only reports. With APP_SEMANTIC_ACTIONS_ENABLED, the gateway asks the
    controller to disable a model whose status is `above_baseline`. Each
    model's status is, in order:
    - `low_coverage`: the window is full enough, but too few requests had a
      Jev answer. Missing data must not look like healthy data.
    - `collecting`: fewer scored samples than the minimum.
    - `no_baseline`: enough samples, but no healthy baseline is configured.
    - `above_baseline`: the disagreement rate is above baseline plus margin.
    - `normal`: otherwise.
    """

    def __init__(self, names: Iterable[BackendName], settings: Settings):
        names = list(names)
        self.windows: dict[BackendName, deque[Sample]] = {
            name: deque(maxlen=settings.semantic_window) for name in names
        }
        # Samples from traffic of the other route, kept out of the windows.
        self.rerouted: dict[BackendName, deque[Sample]] = {
            name: deque(maxlen=settings.semantic_window) for name in names
        }
        self.rules = SemanticRules(
            window=settings.semantic_window,
            min_samples=settings.semantic_min_samples,
            min_coverage=settings.semantic_min_coverage,
            baseline_disagreement_rate=settings.semantic_baseline_disagreement_rate,
            alert_margin=settings.semantic_alert_margin,
            actions_enabled=settings.semantic_actions_enabled,
        )

    def record(self, name: BackendName, sample: Sample, rerouted: bool) -> None:
        (self.rerouted if rerouted else self.windows)[name].append(sample)

    def reset(self, name: BackendName) -> None:
        """Start the model's window again, as the failure monitor does when
        the controller re-enables a model."""
        self.windows[name].clear()

    def stats(self) -> list[SemanticStatus]:
        return [self.status(name) for name in self.windows]

    def status(self, name: BackendName) -> SemanticStatus:
        window = self.windows[name]
        agree, disagree = window.count("agree"), window.count("disagree")
        no_signal = window.count("no_signal")
        scored = agree + disagree
        rate = disagree / scored if scored else None
        coverage = scored / len(window) if window else None
        rules = self.rules
        status: Literal[
            "low_coverage", "collecting", "no_baseline", "above_baseline", "normal"
        ]
        if (
            len(window) >= rules.min_samples
            and coverage is not None
            and coverage < rules.min_coverage
        ):
            status = "low_coverage"
        elif scored < rules.min_samples or rate is None:
            status = "collecting"
        elif rules.baseline_disagreement_rate is None:
            status = "no_baseline"
        elif (
            rate
            > rules.baseline_disagreement_rate
            + rules.alert_margin
            + THRESHOLD_TOLERANCE
        ):
            status = "above_baseline"
        else:
            status = "normal"
        rerouted = self.rerouted[name]
        return SemanticStatus(
            name=name,
            samples=len(window),
            agreements=agree,
            disagreements=disagree,
            no_signal=no_signal,
            disagreement_rate=None if rate is None else round(rate, 3),
            coverage=None if coverage is None else round(coverage, 3),
            status=status,
            rerouted_agreements=rerouted.count("agree"),
            rerouted_disagreements=rerouted.count("disagree"),
            rerouted_no_signal=rerouted.count("no_signal"),
        )
