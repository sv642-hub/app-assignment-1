# Verness’s Jev and Qwen integration plan

## Goal and ownership

Deliver a working backend that routes through real Jev, calls either Qwen backend, and supplies records for Sebastian’s semantic monitor. Start with the **existing 10 banking intents plus `unknown`**. Keep the `out_of_scope` split as a coordinated follow-up.

| Owner | Work |
|---|---|
| Verness | Jev client, routing, Jev intent assessment, gateway records, request deadlines, and wiring the Qwen backends into the gateway |
| Yanzhen | Qwen backends: prompt (classify, read policy card, reply), constrained JSON output, vLLM client with timeouts; per-model accuracy and the always-small/always-large baselines |
| Sebastian | Record agreement, semantic scoring, coverage rules, controller behavior and integration review |
| Pavlos | GPU host, vLLM version and configuration, model URLs, deployment secrets and evaluation runner |
| Jackson | Probe work and its evaluation |

This plan covers only how the gateway calls the Qwen backends and records their results. No open backend implementation PR was visible during planning, but unpublished work still needs coordination. Jev and Deployments screens remain a later milestone.

## 1. Agree contracts and prepare configuration

- Use plain asynchronous `httpx` for Jev and promote it from the `dev` extra to a runtime dependency. Create and close the shared client through the app’s lifespan. Agree with Yanzhen whether the Qwen backends share this lifespan pattern.
- Keep `Router.route(message) → RoutingDecision` and `Backend.generate(message) → BackendReply` as the customer-facing flow’s interfaces. Add an internal assessment/record sink shared with Sebastian; do not make the router return monitoring records instead of its existing decision.
- Adopt PR #45’s records after resolving its validation gaps. Validate probability keys against the requested options, choice membership and highest probability, finite values, ranges, and approximately unit total. Permit ties.
- Agree two changes to PR #45 with Sebastian:
  - Its intent option set has “about 78 options”. This release uses the 10 configured intents plus `unknown` (11 options). `option_set_version` must name the 11-option set.
  - With one combined Jev call (section 2), both `JevAssessment` records come from the same HTTP call. They share `provider_request_id`, `latency_ms` and `input_tokens`. The monitor must not add their tokens twice.
- Create the gateway request ID before any provider call. Today `uuid4()` runs only after the backend replies. Record each actual backend attempt separately, including failed attempts; retain the originally selected route and original backend intent.
- Define `option_set_version` from the canonical label definitions. The backend `version_id` is a Yanzhen + Sebastian interface; the gateway copies it from each backend into its records. Capture both at call time so later configuration changes cannot relabel old results.
- Add Pydantic Settings for mock/real provider selection, URLs, server-side secrets, model IDs and deadlines. Default to mocks; reject incomplete real-mode configuration at startup. Add the new keys, without values, to `env/.env.example` and `env/.env.production.example`.
- Provision a TypeSafe account and key. Choose the Jev model from the SDK’s models listing (`name`, `release_date`) at setup, then pin it. The Choice docs show only `jev-1.13.0` in their examples; `jev-1.14.0` is not documented. Coordinate vLLM URLs, served model names and a pinned server version with Pavlos.

## 2. Implement Jev routing and intent assessment

**One combined call:** Send each message once to TypeSafe’s `/v1/systemone` with two Choice questions. TypeSafe’s docs say to ask every Choice question in a single request, and that questions are evaluated in parallel.

- **`route`:** exactly `standard` and `complex`, with explicit descriptions based on the project’s routing rubric.
- **`intent`:** the same 10 labels and `unknown` used by Qwen. For this release, `unknown` includes both unsupported and ambiguous messages. Jev’s assessment must not overwrite the backend’s answer.

The gateway awaits this call inside the existing **2-second routing deadline**. It makes no automatic retries.

- If the call times out, fails over HTTP, or returns an invalid `route` answer, use the rule router. Record both questions as `timeout` or `error`.
- If the `route` answer is valid but the `intent` answer is missing or invalid, use the route and record only the intent as `error`.
- A routing timeout also loses that request’s intent sample. Timeouts and errors reduce semantic coverage; none count as agreement.

Other rules:

- Measure latency in the gateway. Capture the returned model, token usage and optional `x-typesafe-request-id`. The SDK exposes this header as `request_id`, but the docs do not say it is always present. Verify it with an actual response before claiming live evidence.
- Sebastian’s monitor owns joining and scoring. Supply a recording test sink until that integration is ready; do not turn Jev disagreements into controller actions in Verness’s client work.

**Contingency:** in the live check, measure the combined call’s latency over at least 20 fictional messages. If it often misses the 2-second deadline, split the intent question into its own background call. That design needs a bounded queue (starting at 100 queued assessments, four workers and a 5-second deadline), capacity skips, a five-second drain on shutdown, and a capacity reason for PR #45’s `skipped` status, which today means only “not sampled”. Do not build it unless the measurement requires it.

TypeSafe documents the [Choice request and response](https://docs.typesafe.ai/primitives/choice) and the [request-ID header](https://docs.typesafe.ai/sdk/python/api/types/responses).

## 3. Wire the Qwen backends into the gateway

Yanzhen’s Qwen backends implement the existing `Backend` Protocol. Yanzhen and Pavlos own the prompt, constrained JSON output, vLLM client, per-attempt timeouts and Qwen’s thinking settings. The gateway relies only on the contract below.

**Contract to agree with Yanzhen:**

- `generate` returns a `BackendReply` (`intent`, `reply`, `policy_facts`). An `unknown` reply has no policy facts and asks the customer to describe their banking problem, as the `Backend` Protocol docstring already requires (#24).
- `generate` raises a typed failure for a transport error, a timeout or invalid output (truncated or schema-invalid JSON), so the gateway can record distinct error codes. Exception messages must not contain provider message text, because the gateway logs the exception type.
- Each call exposes the backend `version_id` and the token usage (`input_tokens`, `output_tokens`). `BackendReply` has neither field today, so agree where they go.
- The client makes no hidden retries. The gateway remains responsible for trying the other usable backend once.

**Gateway work (Verness):**

- **Deadlines:** add a configurable **total request deadline** (start at 45 seconds). The gateway gives each `generate` call only the time left, so the alternate attempt is capped. Without it, a customer can wait about 62 seconds (2 s routing + two 30-second attempts) before a 503. Ask Yanzhen to keep the per-attempt client timeout at or below the total.
- Preserve current behavior for the first release: typed failures trigger the alternate backend. Validly parsed replies that fail the gateway’s existing format checks receive its safe apology.
- Record distinct error codes for transport failure, timeout and invalid output. Keep invalid JSON in the existing call-error category until Sebastian and Yanzhen explicitly agree to change its scoring and retry behavior.
- Fill `ModelUsage.input_tokens` and `output_tokens` from each completed call’s token usage. Issue #31 asked for token counts per model; they are `null` in mock mode today.
- **Mock-only fields:** these are hardcoded today and must report real or mixed operation:
  - `Trace.router` is `Literal["mock_jev", "rule_router"]`.
  - `Trace.mocked` is `Literal[True]`.
  - `HealthResponse.mode` is `"mock"`, and `HealthResponse.router` is `"mock_jev"`.

  Keep the mock values for mock mode, and identify Jev and backend provenance separately. The Health screen reads these fields, so this PR also updates the front end and its tests.

## 4. Verification and delivery

Deliver three reviewable PRs: **contracts/configuration**, **Jev integration**, then **Qwen backend wiring and gateway records**. Ask Sebastian to review record changes and Yanzhen to review the backend contract. The third PR can use a stub backend that follows the contract until Yanzhen’s backends are ready.

Required automated scenarios:

- Correct standard/complex routing and exact intent-option handling.
- Missing questions, wrong answer type, missing/extra labels, invalid probabilities and inconsistent choices.
- Jev timeout, authentication failure, rate limiting and malformed responses all preserve customer service through rule routing.
- A valid `route` answer with an invalid `intent` answer uses Jev’s route and records only the intent as an error.
- Backend timeout or invalid JSON triggers one alternate attempt; both failures return 503; disabled models produce no attempted-call record.
- The total request deadline caps the alternate attempt, and a request never runs past it.
- Records retain one request ID, accurate attempt numbers, original route/intent, per-call timing and configuration versions.
- `unknown` carries no policy facts; provider failures and cancellation do not invent successful semantic samples.
- Completed real calls fill `ModelUsage` token counts; mock calls leave them `null`.
- Existing mock-mode behavior, API tests and required repository checks continue to pass.

Then perform separate live checks with fictional messages: authenticate to TypeSafe, list the available Jev models, inspect one combined routing and intent response, measure the combined call’s latency, capture request-ID availability, and, once Yanzhen’s backends are ready, exercise both Qwen backends through the gateway. Report mocked checks and live observations separately.

**Evaluation note:** the shared evaluation set covers all 77 Banking77 intents, but this release has 10 intents plus `unknown`. Messages from 67 of the 77 intents, about 87% of the 300 messages, have the target `unknown`. Overall accuracy and the Jev–Qwen agreement rate would mostly measure that one class. Report per-class recall and macro F1, and score the 10 intents separately from `unknown`.

The release is complete when the real providers work through the gateway, failure paths preserve the agreed fallback behavior, and Sebastian can consume the records. Model accuracy, semantic intervention thresholds, all 77 intents, the new no-match split and UI screens are subsequent milestones.
