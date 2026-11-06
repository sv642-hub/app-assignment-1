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
- **Label spelling:** labels use lowercase letters, digits and underscores only. Banking77 spells two labels differently: `Refund_not_showing_up` (capital R) and `reverted_card_payment?` (question mark), on 40 test messages each. The label file uses `refund_not_showing_up` and `reverted_card_payment`, and keeps each original name in `banking77_name`. Map gold labels by looking up `banking77_name` in the label file, not by lowercasing, which would leave the `?`.
- Keep `config/intents.json` and `config/policy_cards.json` at 10 intents. A card is needed only to reply, not to classify.
- Change the gateway’s `unknown_label` check. Today it flags any intent without a card (`intent not in self.cards`), so a correct uncarded intent would count as a format failure. Check the intent against the 78 labels instead.
- **Evaluation sheet:** keep each message’s original label from the 77 intents, plus `unknown` and `out_of_scope`. Scoring maps labels down to the option set in use; in this release, `out_of_scope` maps to `unknown`. Nothing needs relabelling if the option set changes later. Settle this before anyone writes evaluation messages.

**Interfaces and records**

- Use plain asynchronous `httpx` for Jev and promote it from the `dev` extra to a runtime dependency. Create and close the shared client through the app’s lifespan. Agree with Yanzhen whether the Qwen backends share this lifespan pattern.
- Keep `Backend.generate(message) → BackendReply` as the backend interface.
- **Change the `Router` interface, and the gateway writes the Jev records.** `Router.route(message)` returns only a `RoutingDecision` and gets no request ID, and a call cancelled by the routing deadline cannot write its own record. `AGENTS.md` says to replace mocks “by adding a real implementation, not by editing callers”. This is a deliberate exception, because the gateway needs both answers and the provider details. So:
  - `Router.route(message)` returns an internal `JevResult`: the `route` answer and the `intent` answer, each validated on its own, plus the returned model, token usage and `x-typesafe-request-id`.
  - `MockJevRouter` returns a `JevResult` too, marked as mocked, so mock mode writes the same records. The rule router stays a fallback that returns only a route.
  - Change the interface, both implementations, the gateway and their tests in one PR.
  - The gateway derives the `RoutingDecision` from the `route` answer and writes both `JevAssessment` records with the request ID.
  - On a timeout or cancellation, the gateway writes the timeout records itself.
- **Call ID:** the gateway creates a `call_id` for each Jev HTTP call. Both assessments from one call share it. The monitor groups by `call_id` to count tokens and latency once per call. `provider_request_id` is optional and not reliable enough for this.
- Adopt PR #45’s records after resolving its validation gaps. Validate probability keys against the requested options, choice membership and highest probability, finite values, ranges, and approximately unit total. Permit ties.
- Sebastian will update #45. The changes include:
  - the 78-option intent set, and whether Qwen’s output constraint can use the same 78 labels (a question for Yanzhen);
  - one combined Jev call (timing diagram, `call` field, no “too late” row) and `call_id`;
  - `skipped` allowed on the route question, which the validator rejects today;
  - a `skip_reason` field for `circuit_open`, because `error_type` must be empty on a skipped record;
  - a passed check without a stored reply, for uncarded intents;
  - the validators, and invalid JSON staying a failed call.
- Create the gateway request ID before any provider call. Today `uuid4()` runs only after the backend replies. Record each actual backend attempt separately, including failed attempts; retain the originally selected route and original backend intent.
- The backend `version_id` is a Yanzhen + Sebastian interface; the gateway copies it from each backend into its records. Capture `option_set_version` and `version_id` at call time so later configuration changes cannot relabel old results.

**Settings and accounts**

- Add Pydantic Settings for mock/real provider selection, URLs, server-side secrets, model IDs, deadlines, circuit-breaker limits and Jev’s token price. Default to mocks; reject incomplete real-mode configuration at startup. Add the new keys, without values, to `env/.env.example` and `env/.env.production.example`.
- Provision a TypeSafe account and key. Send the versioned ID `jev-1.14.0` in the `model` field. TypeSafe’s [Models page](https://docs.typesafe.ai/models) documents it, available from October 14, 2026. Do not pick the model from `GET /v1/models`. The Models page says the listing “currently lists the aliases”, such as `jev-latest`, while the API reference describes it as “models and aliases”; nobody has checked a live listing. Either way, an alias moves when a new release ships, so pinning `jev-1.14.0` does not depend on the listing. Confirm the version with one authenticated call, and record each response’s `model` field, which gives the versioned ID that answered.
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
- While the breaker is open, record both questions as `skipped` with `skip_reason` set to `circuit_open`. After the 30 seconds, the next message tries Jev again.
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
- **Intents without a card:** the backend returns the intent, `reply=""` and `policy_facts=[]`, so it generates no text that would be thrown away.
  - The gateway first checks that the intent is one of the 67 recognized uncarded intents. It then inserts a fixed message that the demo does not cover that topic, so no reply invents policy. It discards any text a backend sends anyway.
  - The gateway skips `check_reply` only in this case, because `check_reply` treats an empty reply as a format failure. Schema, label and policy-facts checks still run.
- **`reply_source`:** add separate values for a reply a real model generated and for the fixed uncarded-intent reply. Keep `policy_template` for the mock backends’ templates only.
- `generate` raises a typed failure for a transport error, a timeout or invalid output (truncated or schema-invalid JSON), so the gateway can record distinct error codes. Exception messages must not contain provider message text, because the gateway logs the exception type.
- Each call exposes the backend `version_id` and the token usage (`input_tokens`, `output_tokens`). `BackendReply` has neither field today, so agree where they go.
- The client makes no hidden retries. The gateway remains responsible for trying the other usable backend once.

**Gateway work (Verness):**

- **Deadlines:** add a configurable **total request deadline** (start at 45 seconds) with a **reserve for the alternate** (start at 15 seconds).
  - The first attempt gets the total, minus the reserve, minus the time already used (routing). After 2 seconds of routing, that is 28 seconds.
  - The alternate gets whatever time is left, with no minimum check. The first attempt’s budget already keeps the reserve for it. A check that “at least the reserve remains” would stop the alternate after every first-attempt timeout, because timers fire slightly late.
  - **First attempt timed out:** this is the model’s failure, whichever timer fires first (Yanzhen’s client timeout or the gateway’s budget). The gateway calls `record_error`, and the attempt counts as one outage sample. So a hung model still gets disabled.
  - **Alternate cut short by the time left:** the gateway records `deadline_exceeded`. It gives no sample in either direction, because the model got a shortened chance. Health shows a separate count of these.
  - An alternate that fails on its own before the gateway cuts it counts as a normal failure.
  - If the deadline runs out with no reply, the gateway returns 503 with the reason `deadline_exceeded`.
  - The 30-second per-attempt figure is an assumption from planning. On `main`, backend calls have no timeout. Ask Yanzhen to keep her client timeout at 30 seconds or less; because both timers count the same way on a first attempt, the exact value does not decide whether a hung model is caught.
- Preserve current behavior for the first release: typed failures trigger the alternate backend. Validly parsed replies that fail the gateway’s existing format checks receive its safe apology.
- Record distinct error codes for transport failure, timeout, invalid output and `deadline_exceeded`. Keep invalid JSON in the existing call-error category until Sebastian and Yanzhen explicitly agree to change its scoring and retry behavior.
- **Usage and cost (#31):**
  - Fill `ModelUsage.input_tokens` and `output_tokens` from each completed call’s token usage; they are `null` in mock mode today.
  - Track **Jev’s cost** separately, from its tokens per call and TypeSafe’s token price. The proposal expected two paid Jev calls per request; the combined call makes one, so the report’s cost section changes.
  - **Qwen’s cost** is GPU time, because the models are self-hosted. It is not a token bill. Leave `estimated_cost_usd` `null` for the Qwen models unless the team documents a method for sharing GPU hours across calls.
  - Add token and cost columns to the Health usage table, which has only call counts today.
- **Mock-only fields and text:** these are hardcoded today and must report real or mixed operation:
  - `Trace.router` is `Literal["mock_jev", "rule_router"]`; the Chat details read it (`ChatView.jsx`).
  - `Trace.mocked` is `Literal[True]`, and `Trace.simulated_latency_ms` only makes sense for mocks.
  - `Trace.reply_source` has `policy_template`, which a real model’s reply never is (see the contract above).
  - `HealthResponse.mode` is `"mock"`, and `HealthResponse.router` is `"mock_jev"`.
  - `Sidebar.jsx` shows “Mock mode: Jev and Qwen are simulated”, and the Health usage card says token usage and cost are unavailable.

  Keep the mock values for mock mode, and identify Jev and backend provenance separately. The third delivery PR updates the front end and its tests.
- **User brief:** once real providers run, `USER_BRIEF.md` changes in two places. Sebastian, who owns the docs, proposed the first wording:
  - Replace “Everything is simulated: no real AI models run, and intents are matched by keywords.” with “Real AI models run, and your messages are sent to TypeSafe, an outside service, so use only made-up details.” This takes no extra space.
  - Change “It recognizes ten banking intents” to say it recognizes 77 banking intents and answers 10 of them from policy cards.

  The brief has about one line to spare on its page, so check the length after both changes.

## 4. Verification and delivery

Deliver three reviewable PRs: **contracts/configuration**, **Jev integration**, then **Qwen backend wiring and gateway records**. Ask Sebastian to review record changes and Yanzhen to review the backend contract. The third PR can use a stub backend that follows the contract until Yanzhen’s backends are ready.

Required automated scenarios:

- Correct standard/complex routing and exact intent-option handling for all 78 labels.
- Missing questions, wrong answer type, missing/extra labels, invalid probabilities and inconsistent choices.
- Each question validated on its own: an invalid `route` with a valid `intent` keeps the intent; a valid `route` with an invalid `intent` keeps the route.
- Jev timeout, authentication failure, rate limiting and malformed responses all preserve customer service through rule routing.
- The circuit breaker opens after the configured failures, records `skipped` with `circuit_open`, and closes after the cool-off.
- An uncarded intent with `reply=""` gets the fixed reply, no policy facts and no format failure; an unrecognized label still fails.
- Backend timeout or invalid JSON triggers one alternate attempt; both failures return 503; disabled models produce no attempted-call record.
- The total request deadline, with a hung first model: the first attempt counts as a failure, the alternate still runs with the time left, an alternate cut short records `deadline_exceeded` without a sample, and a request never runs past the deadline.
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
  - Macro F1 is averaged over the **77 gold classes**; the label list passed to the metric is those 77 intents.
  - The official test set has no `unknown` messages, so a prediction of `unknown` counts as wrong for that message’s gold class.
- **Fresh set (sanity check):** it measures `unknown` handling and wording the official set lacks, not per-class accuracy.
  - **Proposal for the team:** each person writes 54 intent messages, 3 vague messages and 3 off-topic messages. That gives 270 intent messages (about 3.5 per intent) and 30 no-match messages. A shared schedule assigns the intents to writers, so all 77 are covered on purpose.
  - Messages keep their original label, and scoring uses the agreed mapping. Sebastian updates the evaluation section of `docs/TEAM_PLAN.md` once the team agrees the numbers. Nobody starts writing before then.
  - With about 3.5 messages per intent, one miss moves a class’s recall by about 25 to 30 points, so make no per-class claims from this set.
- **Class counts:** compute the actual counts of each label in the scoring view from the sheet. Do not assume an even spread.
- **Human reply grades:** a fixed reply is not worth grading, and the fresh set has too few carded-intent messages for 150 grades. Draw the graded replies from Banking77’s **development split**, on the 10 carded intents, so the official test set stays untouched if a prompt or the judge is later tuned on grades. Keep the planned overlap: about 130 distinct replies, 20 of them graded by two people. Report reply quality (task success) for those 10 intents only. `docs/TEAM_PLAN.md` needs the same line.
- **Cost of the evaluation run:** Jev is cheap. Assuming 3,000 input tokens per call, 3,080 messages come to about $0.39 at $0.042 per million tokens. The limit to agree with Pavlos is mostly GPU time for the two Qwen models.

**What the monitor reads:** Jev assesses the customer’s message, not the model’s reply. Sebastian confirmed that this is what the monitor needs: Jev’s intent answer and each model’s answer stay independent. **Limit:** intent agreement cannot detect a correct intent paired with a bad reply. Format checks and the offline judge cover that.

The release is complete when the real providers work through the gateway, failure paths preserve the agreed fallback behavior, and Sebastian can consume the records. Model accuracy, semantic intervention thresholds, policy cards for the other 67 intents, the new no-match split and UI screens are subsequent milestones.
