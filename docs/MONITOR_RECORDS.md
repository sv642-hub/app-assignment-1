# Monitor records (draft)

**Status:** proposal, not built. For review by Verness (Jev assessment) and Yanzhen (backend response), per "Interfaces to agree first" in [TEAM_PLAN.md](TEAM_PLAN.md). Once agreed, these models move into `src/app_assignment_1/schemas.py`.

The monitor reads two kinds of record: one per Jev call and one per backend call. A normal request has three records (two Jev, one backend). A request that falls back has four, because it makes a second backend call. All of them use the same `request_id`, so the monitor can join them even though Jev's intent answer arrives after the customer already has a reply.

```text
request_id created
  ├─ Jev routing call (gateway waits)  ──> JevAssessment (call="routing")
  ├─ Qwen backend call (gateway waits) ──> BackendResponseRecord (attempt=1)
  ├─ Other backend, only if that call failed (gateway waits) ──> BackendResponseRecord (attempt=2)
  └─ Jev intent call (gateway does not wait) ──> JevAssessment (call="intent")
```

Neither record stores the customer's message.

## Jev assessment record

One record per Jev call. Field names follow [Jev's Choice response](https://docs.typesafe.ai/primitives/choice.md): `choice`, `confidence` and `probabilities` come straight from the answer, `jev_model` from `model`, and `input_tokens` from `usage`.

```python
class JevAssessment(StrictModel):
    request_id: UUID
    call: Literal["routing", "intent"]
    status: Literal["ok", "timeout", "error", "skipped"]  # skipped: not sampled
    finished_at: AwareDatetime
    mocked: bool  # keeps mock data out of measured results
    # Which labels Jev chose from. Answers are only comparable within one set.
    option_set_version: str = Field(min_length=1)
    # Measured by the gateway: Jev's docs list no latency field.
    latency_ms: float | None = Field(default=None, ge=0)
    # A short code such as rate_limited, never provider message text.
    error_type: str | None = None
    jev_model: str | None = None  # e.g. jev-1.14.0
    provider_request_id: str | None = None  # x-typesafe-request-id, if available
    input_tokens: int | None = Field(default=None, ge=0)  # cost is computed from this
    choice: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] | None = None

    @model_validator(mode="after")
    def check_call(self) -> "JevAssessment":
        answer = (self.choice, self.confidence, self.probabilities)
        if (self.error_type is not None) != (self.status in ("timeout", "error")):
            raise ValueError("error_type is set exactly when the call failed")
        if self.status != "ok":
            if any(value is not None for value in answer):
                raise ValueError("Only an ok call has an answer")
            if self.call == "routing" and self.status == "skipped":
                raise ValueError("The routing call is never sampled")
            return self
        if (
            self.choice is None
            or self.confidence is None
            or self.probabilities is None
            or self.jev_model is None
            or self.input_tokens is None
        ):
            raise ValueError("An ok call needs its answer, model and tokens")
        if self.choice not in self.probabilities:
            raise ValueError("The choice must be one of the options")
        if not all(0 <= value <= 1 for value in self.probabilities.values()):
            raise ValueError("Each probability must be between 0 and 1")
        if abs(sum(self.probabilities.values()) - 1) > 0.05:
            raise ValueError("Probabilities should sum to about 1")
        if self.call == "routing" and set(self.probabilities) != {
            "standard",
            "complex",
        }:
            raise ValueError("The routing call chooses standard or complex")
        return self
```

Jev's confidence only summarizes the top probability, and the formula depends on the number of options. A routing confidence (2 options) and an intent confidence (about 78 options) are therefore not comparable, and neither is a calibrated probability.

## Backend response record

One record per backend call, so up to two per request. `model` is the model that was called. `attempt` is 1 for the first call made for the request and 2 for the fallback. A disabled backend is skipped without a call, so it writes no record and the other backend's call is attempt 1.

```python
class BackendResponseRecord(StrictModel):
    request_id: UUID
    model: BackendName
    attempt: Literal[1, 2]
    # Prompt, policy cards and output format in use. Canary and rollback need this.
    version_id: str = Field(min_length=1)
    option_set_version: str = Field(min_length=1)
    selected_route: Route  # the router's choice, before any fallback
    route_source: Literal["jev", "rule_router"]
    status: Literal["ok", "timeout", "error"]  # did the call return at all?
    error_type: str | None = None
    finished_at: AwareDatetime
    mocked: bool
    latency_ms: float = Field(ge=0)  # the backend call only, not the whole request
    intent: str | None = None  # what the model said, before any safe fallback
    reply: str | None = None  # None when the format check fails: never store it
    format_check: Literal["passed", "failed"] | None = None  # None if no return
    format_errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_outcome(self) -> "BackendResponseRecord":
        if (self.error_type is not None) != (self.status != "ok"):
            raise ValueError("error_type is set exactly when the call failed")
        if self.status != "ok":
            if self.intent or self.reply or self.format_check or self.format_errors:
                raise ValueError("A call that did not return has no output")
        elif self.format_check == "passed":
            if self.intent is None or self.reply is None or self.format_errors:
                raise ValueError("A passed check needs an intent and a reply")
        elif self.format_check == "failed":
            if self.reply is not None or not self.format_errors:
                raise ValueError("A failed check has errors and no stored reply")
        else:
            raise ValueError("A call that returned needs a format check")
        return self
```

## How the monitor scores a request

Each backend record is scored on its own, against the request's intent assessment. A request that fell back gives one outage sample for the first model, then one more row for the second. The gateway stops once a call returns, so today at most one backend record per request has `status="ok"`, and a request gives at most one agreement or disagreement.

| Backend record | Intent assessment | The monitor counts |
|---|---|---|
| `timeout` or `error` | any | One outage sample. No disagreement sample. |
| Format check failed | any | One format failure. No disagreement sample. |
| Format check passed | `ok`, same label | One agreement |
| Format check passed | `ok`, different label | One disagreement |
| Format check passed | `timeout`, `error`, `skipped`, or too late | No semantic sample, and lower coverage |

- **Different label sets:** if `option_set_version` differs between the two records, the pair is not scored.
- **Rerouted traffic:** a model covering the other route sees different messages, so its normal rate may not apply. When `selected_route` is not the model's own route, those samples are tracked separately.
- **Low coverage:** Health shows "no semantic signal" when too few requests have a Jev answer. During a Jev outage, missing data must not look like healthy data.
- **Canary comparison:** rates are grouped by `model` and `version_id`, so a canary can be compared with the version it would replace.

The window, thresholds, coverage minimum and how long to wait for a late answer are not chosen here. They should be set from dev data and frozen before the final test.

## What must change in the gateway

These are gaps in the current code that the records depend on.

1. **Failed calls leave no record.** `Gateway.chat` now catches errors from `backend.generate`, tries the other backend and counts the failure toward the controller's decision, but it writes no record of the call. It must write one with `status` set to `timeout` or `error`. A call that never returns is still not detected, so the real client must time out and raise.
2. **`request_id` comes too late.** It is created when the response is built, but it must exist before the first Jev call.
3. **The model's intent is overwritten.** After a failed format check, `intent` becomes `unknown` for the customer's response. The record must be written with the model's original intent.
4. **The router's choice is lost.** `Trace.route` holds the route of the backend that served the request, and `jev_choice` is empty when the rule router chose. `selected_route` and `route_source` keep this.
5. **Latency covers the whole request.** `Trace.latency_ms` includes routing. `latency_ms` on the backend record covers only the backend call.

## Mock mode

Both mock backends share one keyword classifier. A mock intent call using that same classifier would always agree with them, so a 0% disagreement rate in mock mode says nothing about quality. Mock records set `mocked=True`, and a chaos fault that swaps labels is needed to show the monitor working. That fault would also answer issue #32, which asks for a way to see declining quality.

## Answered by Verness

1. **The intent call includes an `unknown` option.** Jev must choose from the options it is given, so without one, every no-match reply counts as a disagreement and inflates the rate. It uses the same label set as the backends: the planned 77 Banking77 intents plus `unknown`, so 78 options, within Jev's limit of 255. `config/intents.json` has 10 intents today. Jev and the backends need one definition of `unknown`. Proposal: "not one of the banking topics", rather than "banking, but unclear".
2. **Two Jev calls, not one.** One combined call would put the 78-option intent question on the path the customer waits for, behind the 2-second routing timeout.

## Questions to settle

**Verness**

1. Plain `httpx` or the SDK? Verness is leaning towards `httpx` and will check one live HTTP response for `x-typesafe-request-id`, which Jev documents in its SDK docs, not on its HTTP API page. `provider_request_id` is optional, so the record works either way.

**Yanzhen**

1. Can `generate` raise separate errors for a timeout and a failed call, so the gateway can set `status` and `error_type`?
2. How should invalid or cut-off JSON be reported? Today a reply that does not match `BackendReply` counts as a failed call (`call_errors`), not a format failure, and the request is tried on the other backend. Proposal: `status="ok"`, `format_check="failed"`, and `invalid_json` in `format_errors`, since the model did answer. This is the "broken output format" fault in the evaluation plan. The proposal would require changing `Gateway.chat`, and the customer would get the safe apology instead of a reply from the other backend unless that retry is kept. If it is kept, a request could have two `ok` backend records, and the scoring section would need to say which one counts.
3. Can the output constraint include `unknown`, using the same labels and definition as the Jev intent call?
4. Who sets `version_id`: the backend from its config, or the gateway per request for canary traffic? This also needs Verness, who owns the Deployments screen.
5. With Jackson: does the probe gate need the model's token-probability confidence on this record, or only in offline dev runs?
