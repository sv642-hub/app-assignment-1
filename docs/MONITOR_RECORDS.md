# Monitor records (draft)

**Status:** proposal, not built. For review by Verness (Jev assessment) and Yanzhen (backend response), per "Interfaces to agree first" in [TEAM_PLAN.md](TEAM_PLAN.md). It follows the design agreed in [JEV_QWEN_INTEGRATION_PLAN.md](JEV_QWEN_INTEGRATION_PLAN.md). Once agreed, these models move into `src/app_assignment_1/schemas.py`.

The monitor reads two kinds of record: one per Jev question and one per backend call. Each message goes to Jev once, with two questions, `route` and `intent`, so a normal request has three records: two Jev assessments from the same call, and one backend response. A request that falls back has four, because it makes a second backend call. All of them share the gateway's `request_id`, created before any provider call.

```text
request_id created
  ├─ Jev call (gateway waits, 2 s routing deadline)
  │    ├─ JevAssessment (question="route")
  │    └─ JevAssessment (question="intent")      same call_id
  ├─ Qwen backend call (gateway waits) ──> BackendResponseRecord (attempt=1)
  └─ Other backend, only if that call failed ──> BackendResponseRecord (attempt=2)
```

Jev assesses the customer's message, not the model's reply, so its intent answer and the model's stay independent. **Limit:** intent agreement cannot catch a correct intent with a bad reply. Format checks and the offline judge cover that.

Neither record stores the customer's message.

## Jev assessment record

One record per question. Field names follow [Jev's Choice response](https://docs.typesafe.ai/primitives/choice.md): `choice`, `confidence` and `probabilities` come straight from the answer, `jev_model` from `model`, and `input_tokens` from `usage`.

```python
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class JevAssessment(StrictModel):
    request_id: UUID
    # One per Jev HTTP call, made by the gateway. Both questions from a call
    # share it, and so do latency_ms and input_tokens: count those once per
    # call_id, not once per record.
    call_id: UUID
    question: Literal["route", "intent"]
    status: Literal["ok", "timeout", "error", "skipped"]
    skip_reason: Literal["circuit_open"] | None = None
    finished_at: AwareDatetime
    mocked: bool  # keeps mock data out of measured results
    # Which options Jev chose from. Answers are only comparable within one set.
    # Intent: the version derived from config/labels.json. Route: a fixed name.
    option_set_version: str = Field(min_length=1)
    # Measured by the gateway: Jev's docs list no latency field.
    latency_ms: float | None = Field(default=None, ge=0)
    # A short code such as rate_limited or invalid_answer, never provider text.
    error_type: str | None = None
    jev_model: str | None = None  # the versioned ID that answered, e.g. jev-1.14.0
    provider_request_id: str | None = None  # x-typesafe-request-id, if present
    input_tokens: int | None = Field(default=None, ge=0)
    choice: str | None = None
    confidence: Probability | None = None
    probabilities: dict[str, Probability] | None = None

    @model_validator(mode="after")
    def check_answer(self) -> "JevAssessment":
        answer = (self.choice, self.confidence, self.probabilities)
        if (self.error_type is not None) != (self.status in ("timeout", "error")):
            raise ValueError("error_type is set exactly when the question failed")
        if (self.skip_reason is not None) != (self.status == "skipped"):
            raise ValueError("skip_reason is set exactly when the question was skipped")
        if self.status != "ok":
            if any(value is not None for value in answer):
                raise ValueError("Only an ok question has an answer")
            return self
        if None in answer or self.jev_model is None or self.input_tokens is None:
            raise ValueError("An ok question needs its answer, model and tokens")
        probabilities = self.probabilities or {}
        if self.choice not in probabilities:
            raise ValueError("The choice must be one of the options")
        if probabilities[self.choice] < max(probabilities.values()) - 1e-9:
            raise ValueError("The choice must have the highest probability")
        if abs(sum(probabilities.values()) - 1) > 0.05:
            raise ValueError("Probabilities should sum to about 1")
        if self.question == "route" and set(probabilities) != {"standard", "complex"}:
            raise ValueError("The route question chooses standard or complex")
        return self
```

The record checks what it can on its own. Two more checks need the request, so the gateway makes them before writing an `ok` record:

- **Same options:** the probability keys must equal the options it asked for: the 78 labels for `intent`. If they don't, the question is recorded as `error` with `error_type="invalid_answer"`.
- **Each question on its own:** a valid `intent` answer is kept even when `route` is invalid, and the other way round.

| What happened | `route` record | `intent` record |
|---|---|---|
| Both answers valid | `ok` | `ok` |
| Whole call failed (timeout, HTTP error, unreadable body) | `timeout` or `error` | `timeout` or `error` |
| `route` invalid, `intent` valid | `error` (rule router used) | `ok` |
| `route` valid, `intent` invalid | `ok` | `error` |
| Circuit breaker open | `skipped`, `circuit_open` | `skipped`, `circuit_open` |

Ties are allowed: the choice only needs a probability equal to the highest. Jev's confidence summarizes how the probabilities are spread, and the formula depends on the number of options. A route confidence (2 options) and an intent confidence (78 options) are therefore not comparable, and neither is a calibrated probability.

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
    # deadline_exceeded: the gateway cut a fallback short for lack of time left.
    status: Literal["ok", "timeout", "error", "deadline_exceeded"]
    # transport, timeout, invalid_output or deadline_exceeded; never provider text.
    error_type: str | None = None
    finished_at: AwareDatetime
    mocked: bool
    latency_ms: float = Field(ge=0)  # the backend call only, not the whole request
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    intent: str | None = None  # what the model said, before any safe fallback
    # Stored only for a passed check with a generated reply. None for an
    # uncarded intent, whose reply the gateway replaces, and for a failed check.
    reply: str | None = None
    reply_source: (
        Literal[
            "policy_template",
            "generated",
            "no_match",
            "unsupported_topic",
            "format_fallback",
        ]
        | None
    ) = None
    format_check: Literal["passed", "failed"] | None = None  # None if no return
    format_errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_outcome(self) -> "BackendResponseRecord":
        output = (self.intent, self.reply, self.reply_source, self.format_check)
        if (self.error_type is not None) != (self.status != "ok"):
            raise ValueError("error_type is set exactly when the call failed")
        if self.status == "deadline_exceeded" and self.attempt != 2:
            raise ValueError("Only a fallback can be cut short by the deadline")
        if self.status != "ok":
            if any(value is not None for value in output) or self.format_errors:
                raise ValueError("A call that did not return has no output")
        elif self.format_check == "passed":
            if self.intent is None or self.format_errors:
                raise ValueError("A passed check needs an intent and no errors")
            if self.reply_source in (None, "format_fallback"):
                raise ValueError("A passed check needs its reply source")
            if (self.reply is None) != (self.reply_source == "unsupported_topic"):
                raise ValueError("Store the reply, unless the intent has no card")
        elif self.format_check == "failed":
            if self.reply is not None or not self.format_errors:
                raise ValueError("A failed check has errors and no stored reply")
            if self.reply_source != "format_fallback":
                raise ValueError("A failed check's reply source is format_fallback")
        else:
            raise ValueError("A call that returned needs a format check")
        return self
```

- **Invalid JSON** (truncated or not matching the schema) stays a failed call: `status="error"`, `error_type="invalid_output"`, and the request is tried on the other backend, as it is today.
- **A first attempt that times out** is the model's failure, whichever timer fires first (the backend client's or the gateway's): `status="timeout"`.
- **A fallback cut short** because the request deadline ran out is `status="deadline_exceeded"`. The model only had the time that was left, so it gives no sample in either direction.
- **`reply_source`** follows the gateway's trace. `generated` is the planned value for a real model's reply; `policy_template` stays for the mock backends.

## How the monitor scores a request

Each backend record is scored on its own, against the request's `intent` assessment. A request that fell back gives one outage sample for the first model, then one more row for the second. The gateway stops once a call returns, so at most one backend record per request has `status="ok"`, and a request gives at most one agreement or disagreement.

| Backend record | Intent assessment | The monitor counts |
|---|---|---|
| `timeout` or `error` | any | One outage sample. No disagreement sample. |
| `deadline_exceeded` | any | No sample. A separate count on Health. |
| Format check failed | any | One format failure. No disagreement sample. |
| Format check passed | `ok`, same label | One agreement |
| Format check passed | `ok`, different label | One disagreement |
| Format check passed | `timeout`, `error` or `skipped` | No semantic sample, and lower coverage |

- **`unknown` and uncarded intents** are labels like any other: `unknown` against `unknown` is an agreement, and an uncarded intent (`unsupported_topic`) is scored on its label.
- **Different option sets:** if `option_set_version` differs between the two records, the pair is not scored.
- **Rerouted traffic:** a model covering the other route sees different messages, so its normal rate may not apply. When `selected_route` is not the model's own route, those samples are tracked separately.
- **Low coverage:** Health shows "no semantic signal" when too few requests have a Jev answer. During a Jev outage or with the breaker open, missing data must not look like healthy data.
- **Canary comparison:** rates are grouped by `model` and `version_id`, so a canary can be compared with the version it would replace.
- **Jev cost and latency:** both records from a call carry the same `latency_ms` and `input_tokens`. Group by `call_id` and count each call once.

The window, thresholds and coverage minimum are not chosen here. They should be set from dev data and frozen before the final test.

## What must change in the gateway

These are gaps in the current code that the records depend on.

1. **Failed calls leave no record.** `Gateway.chat` catches errors from `backend.generate`, tries the other backend and counts the failure toward the controller's decision, but it writes no record of the call. It must write one with the right `status` and `error_type`.
2. **`request_id` comes too late on `main`.** It is created when the response is built. #68 moves it before any provider call.
3. **The model's intent is overwritten.** After a failed format check, `intent` becomes `unknown` for the customer's response. The record must keep the model's original intent.
4. **The router's choice is lost.** `Trace.route` holds the route of the backend that served the request, and `jev_choice` is empty when the rule router chose. `selected_route` and `route_source` keep this.
5. **Latency covers the whole request.** `Trace.latency_ms` includes routing. `latency_ms` on the backend record covers only the backend call.
6. **No call has a time limit yet.** The request deadline and the `deadline_exceeded` status come with the backend wiring in the plan's third delivery PR.

## Mock mode

Both mock backends share one keyword classifier. A mock `intent` answer from that same classifier would always agree with them, so a 0% disagreement rate in mock mode says nothing about quality. Mock records set `mocked=True`, and a chaos fault that swaps labels is needed to show the monitor working. That fault would also answer issue #32, which asks for a way to see declining quality.

## Settled so far

- **`unknown` is an option** (Verness). It covers non-banking and ambiguous messages. The option set is the 77 Banking77 intents plus `unknown`, 78 in all, from `config/labels.json` (#68). `out_of_scope` exists only in the evaluation sheet and maps to `unknown` in this release.
- **One combined Jev call** (#61). This replaces the earlier proposal of two calls, so there is no late intent answer to wait for. A separate background intent call is a contingency only if live timeouts exceed 5%.
- **Plain `httpx`**, not the SDK (#61). `provider_request_id` stays optional.
- **Invalid JSON stays a failed call** (#61), until Yanzhen and Sebastian agree to change its scoring and retry behavior.
- **Token-probability confidence is not on the live record** (Jackson). The probe gate runs on offline dev runs.

## Questions to settle

**Yanzhen**

1. Can `generate` raise separate typed errors for a transport failure, a timeout and invalid output, so the gateway can set `status` and `error_type`?
2. Can Qwen's output constraint use the same 78 labels from `config/labels.json`?
3. Who sets `version_id`: the backend from its config, or the gateway per request for canary traffic? This also needs Verness, who owns the Deployments screen.
4. Where do token counts go? `BackendReply` has neither `version_id` nor token usage today.
