# Verness’s Jev and Qwen integration plan

## Goal and ownership

Deliver a working backend that routes through real Jev, calls either Qwen backend, and supplies records for Sebastian’s semantic monitor.

**Scope: classify 77, reply 10.** Jev and Qwen classify each message into the **77 Banking77 intents plus `unknown`** (78 options). Qwen replies from a policy card only for the **10 intents that have cards**. For the other 67 intents, the customer gets a fixed reply that the demo does not cover that topic. This keeps the proposal’s primary metric, intent accuracy with macro F1 on the official test set, without writing 67 more policy cards. The `out_of_scope` split stays a follow-up.

| Owner | Work |
|---|---|
| Verness | Jev client, routing, circuit breaker, Jev intent assessment, gateway records, request deadlines, the shared label file, and wiring the Qwen backends into the gateway |
| Yanzhen | Qwen backends: prompt (classify, read policy card, reply), constrained JSON output, vLLM client with timeouts; per-model accuracy and the always-small/always-large baselines |
| Sebastian | Record agreement, semantic scoring, coverage rules, controller behavior and integration review |
| Pavlos | GPU host, vLLM version and configuration, model URLs, deployment secrets and evaluation runner |
| Jackson | Probe work and its evaluation |

This plan covers only how the gateway calls the Qwen backends and records their results. No open backend implementation PR was visible during planning, but unpublished work still needs coordination. Jev and Deployments screens remain a later milestone.

## 1. Agree contracts and prepare configuration

**Labels**

- Add one label file with the 77 Banking77 intents and `unknown`, each with a short description. Jev’s `intent` question and Qwen’s intent enum both read it. `option_set_version` is derived from this file.
- Keep `config/intents.json` and `config/policy_cards.json` at 10 intents. A card is needed only to reply, not to classify.
- Change the gateway’s `unknown_label` check. Today it flags any intent without a card (`intent not in self.cards`), so a correct uncarded intent would count as a format failure. Check the intent against the 78 labels instead.
- **Evaluation sheet:** keep each message’s original label from the 77 intents, plus `unknown` and `out_of_scope`. Scoring maps labels down to the option set in use; in this release, `out_of_scope` maps to `unknown`. Nothing needs relabelling if the option set changes later. Settle this before anyone writes evaluation messages.

**Interfaces and records**

- Use plain asynchronous `httpx` for Jev and promote it from the `dev` extra to a runtime dependency. Create and close the shared client through the app’s lifespan. Agree with Yanzhen whether the Qwen backends share this lifespan pattern.
- Keep `Backend.generate(message) → BackendReply` as the backend interface.
- **The gateway writes the Jev records.** `Router.route(message)` gets no request ID, and a call cancelled by the routing deadline cannot write its own record. So:
  - The Jev client returns an internal `JevResult` to the gateway: the `route` answer and the `intent` answer, each validated on its own, plus the returned model, token usage and `x-typesafe-request-id`.
  - The gateway derives the `RoutingDecision` from the `route` answer and writes both `JevAssessment` records with the request ID.
  - On a timeout or cancellation, the gateway writes the timeout records itself.
- **Call ID:** the gateway creates a `call_id` for each Jev HTTP call. Both assessments from one call share it. The monitor groups by `call_id` to count tokens and latency once per call. `provider_request_id` is optional and not reliable enough for this.
- Adopt PR #45’s records after resolving its validation gaps. Validate probability keys against the requested options, choice membership and highest probability, finite values, ranges, and approximately unit total. Permit ties.
- Sebastian will update #45 once the scope and labelling above are agreed. The changes include the 78-option intent set, one combined Jev call (timing diagram, `call` field, no “too late” row), `call_id`, the `skipped` status (now used by the circuit breaker), the validators, and invalid JSON staying a failed call.
- Create the gateway request ID before any provider call. Today `uuid4()` runs only after the backend replies. Record each actual backend attempt separately, including failed attempts; retain the originally selected route and original backend intent.
- The backend `version_id` is a Yanzhen + Sebastian interface; the gateway copies it from each backend into its records. Capture `option_set_version` and `version_id` at call time so later configuration changes cannot relabel old results.

**Settings and accounts**

- Add Pydantic Settings for mock/real provider selection, URLs, server-side secrets, model IDs, deadlines and circuit-breaker limits. Default to mocks; reject incomplete real-mode configuration at startup. Add the new keys, without values, to `env/.env.example` and `env/.env.production.example`.
- Provision a TypeSafe account and key. Send the versioned ID `jev-1.14.0` in the `model` field. TypeSafe’s [Models page](https://docs.typesafe.ai/models) documents it, available from October 14, 2026. Do not pick the model from `GET /v1/models`: it currently lists only aliases such as `jev-latest`, and an alias moves when a new release ships. Confirm the version with one authenticated call, and record each response’s `model` field, which gives the versioned ID that answered.
- Coordinate vLLM URLs, served model names and a pinned server version with Pavlos.

## 2. Implement Jev routing and intent assessment

**One combined call:** send each message once to TypeSafe’s `/v1/systemone` with two Choice questions. TypeSafe’s docs say to ask every Choice question in a single request, and that questions are evaluated in parallel.

- **`route`:** exactly `standard` and `complex`, with explicit descriptions based on the project’s routing rubric.
- **`intent`:** the 78 labels from the label file. `unknown` covers non-banking and ambiguous messages. Jev’s assessment must not overwrite the backend’s answer.

The gateway awaits this call inside the existing **2-second routing deadline**. It makes no automatic retries.

**Validate each question on its own:**

- **Whole-call failure** (timeout, HTTP error, or a body that cannot be parsed): use the rule router, and record both questions as `timeout` or `error`.
- **Invalid `route`, valid `intent`:** use the rule router, record the route as `error`, and keep the intent assessment.
- **Valid `route`, invalid `intent`:** use Jev’s route, and record only the intent as `error`.
- A routing timeout also loses that request’s intent sample. Timeouts and errors reduce semantic coverage; none count as agreement.

**Circuit breaker:** the proposal says a circuit breaker switches to the rule router when Jev fails. Without one, every message waits the full 2 seconds during a Jev outage.

- After 5 consecutive whole-call failures, skip Jev for 30 seconds and use the rule router. Both limits are configurable starting values.
- While the breaker is open, record both questions as `skipped` with the reason `circuit_open`. After the 30 seconds, the next message tries Jev again.
- Report the breaker state in `/v1/health`, so the Health screen can show that routing is degraded.

**Other rules:**

- Measure latency in the gateway. Capture the returned model, token usage and optional `x-typesafe-request-id`. The SDK exposes this header as `request_id`, but the docs do not say it is always present. Verify it with an actual response before claiming live evidence.
- Sebastian’s monitor owns joining and scoring. Supply a recording test sink until that integration is ready; do not turn Jev disagreements into controller actions in Verness’s client work.

**Contingency:** split the intent question into its own background call only if the combined call misses the routing deadline too often.

- **Trigger:** more than 5% of combined calls time out, over at least 100 fictional messages. Report the timeout rate by message length, because long messages may time out more.
- A 20-message run is only a smoke check. It cannot estimate tail latency.
- Repeat the measurement whenever the option list changes.
- The split design needs a bounded queue (starting at 100 queued assessments, four workers and a 5-second deadline), capacity skips, a five-second drain on shutdown, and a capacity reason for `skipped`. Do not build it unless the trigger is met.

TypeSafe documents the [Choice request and response](https://docs.typesafe.ai/primitives/choice), the [request-ID header](https://docs.typesafe.ai/sdk/python/api/types/responses) and the [models](https://docs.typesafe.ai/models).

## 3. Wire the Qwen backends into the gateway

Yanzhen’s Qwen backends implement the existing `Backend` Protocol. Yanzhen and Pavlos own the prompt, constrained JSON output, vLLM client, per-attempt timeouts and Qwen’s thinking settings. The gateway relies only on the contract below.

**Contract to agree with Yanzhen:**

- `generate` returns a `BackendReply` (`intent`, `reply`, `policy_facts`). The intent is one of the 78 labels.
- An `unknown` reply has no policy facts and asks the customer to describe their banking problem, as the `Backend` Protocol docstring already requires (#24).
- **Intents without a card:** the backend returns the intent and no policy facts. The gateway replaces the reply with a fixed message that the demo does not cover that topic, so no reply invents policy. The trace gets a new `reply_source` value for this case.
- `generate` raises a typed failure for a transport error, a timeout or invalid output (truncated or schema-invalid JSON), so the gateway can record distinct error codes. Exception messages must not contain provider message text, because the gateway logs the exception type.
- Each call exposes the backend `version_id` and the token usage (`input_tokens`, `output_tokens`). `BackendReply` has neither field today, so agree where they go.
- The client makes no hidden retries. The gateway remains responsible for trying the other usable backend once.

**Gateway work (Verness):**

- **Deadlines:** add a configurable **total request deadline** (start at 45 seconds) with a **reserve for the alternate** (start at 15 seconds).
  - The first attempt gets at most the total minus the reserve (30 seconds). If it finishes early, the alternate gets all the time left.
  - The alternate runs only if at least the reserve remains.
  - When the gateway cuts an attempt short, it records `deadline_exceeded`. This does not count toward the controller: the gateway does not call `record_error`, because the model may be healthy and only slower than the time left. Agree the scoring of these attempts with Sebastian.
  - If the deadline runs out with no reply, the gateway returns 503 with the reason `deadline_exceeded`.
  - The 30-second per-attempt figure is an assumption from planning. On `main`, backend calls have no timeout. Ask Yanzhen to keep her client timeout at or below the first attempt’s budget.
- Preserve current behavior for the first release: typed failures trigger the alternate backend. Validly parsed replies that fail the gateway’s existing format checks receive its safe apology.
- Record distinct error codes for transport failure, timeout, invalid output and `deadline_exceeded`. Keep invalid JSON in the existing call-error category until Sebastian and Yanzhen explicitly agree to change its scoring and retry behavior.
- **Usage and cost (#31):**
  - Fill `ModelUsage.input_tokens` and `output_tokens` from each completed call’s token usage; they are `null` in mock mode today.
  - Fill `estimated_cost_usd` from configured per-token prices, and leave it `null` when no price is configured.
  - Add token and cost columns to the Health usage table, which has only call counts today.
  - Track Jev’s tokens per call the same way. The proposal expected two paid Jev calls per request; the combined call makes one, so the report’s cost section changes.
- **Mock-only fields and text:** these are hardcoded today and must report real or mixed operation:
  - `Trace.router` is `Literal["mock_jev", "rule_router"]`; the Chat details read it (`ChatView.jsx`).
  - `Trace.mocked` is `Literal[True]`, and `Trace.simulated_latency_ms` only makes sense for mocks.
  - `HealthResponse.mode` is `"mock"`, and `HealthResponse.router` is `"mock_jev"`.
  - `Sidebar.jsx` shows “Mock mode: Jev and Qwen are simulated”, and the Health usage card says token usage and cost are unavailable.

  Keep the mock values for mock mode, and identify Jev and backend provenance separately. The third delivery PR updates the front end and its tests.
- **User brief:** `USER_BRIEF.md` says no real AI models run. Once real providers run, it must say so and tell testers that their messages go to TypeSafe. The brief has about one line to spare on its page, so agree the wording with Sebastian, who owns the docs.

## 4. Verification and delivery

Deliver three reviewable PRs: **contracts/configuration**, **Jev integration**, then **Qwen backend wiring and gateway records**. Ask Sebastian to review record changes and Yanzhen to review the backend contract. The third PR can use a stub backend that follows the contract until Yanzhen’s backends are ready.

Required automated scenarios:

- Correct standard/complex routing and exact intent-option handling for all 78 labels.
- Missing questions, wrong answer type, missing/extra labels, invalid probabilities and inconsistent choices.
- Each question validated on its own: an invalid `route` with a valid `intent` keeps the intent; a valid `route` with an invalid `intent` keeps the route.
- Jev timeout, authentication failure, rate limiting and malformed responses all preserve customer service through rule routing.
- The circuit breaker opens after the configured failures, records `skipped` with `circuit_open`, and closes after the cool-off.
- An uncarded intent gets the fixed reply, no policy facts and no format failure.
- Backend timeout or invalid JSON triggers one alternate attempt; both failures return 503; disabled models produce no attempted-call record.
- The total request deadline: the alternate runs only with the reserve left, an attempt cut short records `deadline_exceeded` without counting toward the controller, and a request never runs past the deadline.
- Records retain one request ID, one `call_id` per Jev call, accurate attempt numbers, original route/intent, per-call timing and configuration versions.
- `unknown` carries no policy facts; provider failures and cancellation do not invent successful semantic samples.
- Completed real calls fill `ModelUsage` token counts; mock calls leave them `null`.
- Existing mock-mode behavior, API tests and required repository checks continue to pass.

**Live checks** with fictional messages. Report mocked checks and live observations separately.

- Authenticate to TypeSafe, send `jev-1.14.0`, and confirm the returned `model`.
- Inspect one combined routing and intent response, and capture request-ID availability.
- Measure the combined call’s timeout rate as described in the contingency.
- **`unknown` handling:** TypeSafe warns that a `none of the above` option “may not work well in many use cases” and suggests a companion Noul question in the same request. On messages that match none of the 77 intents, compare `unknown` as a Choice option with a companion Noul. Also test whether option order changes Jev’s answers.
- Once Yanzhen’s backends are ready, exercise both Qwen backends through the gateway.

**Evaluation**

- **Primary:** intent accuracy and macro F1 on the official Banking77 test set (3,080 messages, 40 per intent), as the proposal states. Report per-class recall there, where each class has enough messages.
- **Fresh set (sanity check):** 300 messages over 77 intents is about 4 per intent, so one miss moves a class’s recall by about 25 points. Use it to check `unknown` handling and wording the official set lacks, not for per-class claims.
- **Class counts:** compute the actual counts of each label in the scoring view from the sheet. Do not assume an even spread.
- Running the official test set through Jev and both models costs tokens. Agree a spending limit with Pavlos before the run.

**Open question for Sebastian:** the proposal says the second Jev call scores “the answer”. This plan has Jev assess the customer’s message, not the model’s reply. Confirm that this is what the monitor needs.

The release is complete when the real providers work through the gateway, failure paths preserve the agreed fallback behavior, and Sebastian can consume the records. Model accuracy, semantic intervention thresholds, policy cards for the other 67 intents, the new no-match split and UI screens are subsequent milestones.
