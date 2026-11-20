# Proposal: measure and reduce Jev routing position bias

Status: proposed for discussion; no runtime changes or measured results in this PR.

## Problem and scope

The professor raised a conference report that Jev may favor the first routing
option. We have not yet established the size or direction of this effect in our
gateway. The hypothesis is that an otherwise identical request receives a
different route when the options change position.

In the current client, `jev.py` presents `standard` before `complex` in
`ROUTE_CRITERIA`. Jev classifies the request's complexity; the gateway maps that
route to a Qwen backend. It does not directly compare names such as Model A and
Model B. The client combines route and intent questions in one call.

We propose randomized routing order, an offline reversed-order experiment, and
separately validated downstream quality checks. Randomization balances exposure
to the first position; it does not guarantee an unbiased individual decision.
The target is reliable routing and good replies at acceptable cost, not a 50/50
split between models. This proposal does not change ownership or built/simulated
architecture status. Implementation PRs must update the architecture YAML and
generated SVG when they change that status.

## 1. Verify the ordering mechanism and measure a baseline

Before implementing live randomization, confirm with the provider or a supported
inspection mechanism that ordering the Choice criteria changes the order Jev
actually sees. JSON object order alone is not a provider guarantee. If TypeSafe
normalizes criteria order, use a provider-supported ordering control; do not
claim that shuffling our payload changes the underlying prompt.

Use development examples to establish the procedure. For every example, run
both `standard, complex` and `complex, standard` with identical descriptions,
instructions, intent options, and model settings. Randomize execution order and
repeat each ordering to estimate same-order variability. Fix and record the
repeat count and budget before the final run. Decode all choices and scores by
canonical route name, never by position. A separate alias experiment can later
test label-name preference; do not confound it with the initial order experiment.

Report these measurements with sample counts and uncertainty intervals:

| Measure | Meaning |
|---|---|
| Reversed-order flip rate | Fraction of valid paired calls choosing different canonical routes |
| Same-order disagreement | Variability when the exact same request and order are repeated |
| First-position selection rate | Fraction selecting the first presented route across balanced orders |
| Standard-route selection difference | P(standard when first) minus P(standard when second) on the same examples |
| Quality and operational impact | Reply correctness, policy adherence, escalation rate, tokens, cost when available, and p50/p95 latency |

Count errors, timeouts, and incomplete pairs separately; do not silently drop
them. Bootstrap by example, keeping repeated calls together. Interpret flips
alongside ordinary variability; a flip alone does not prove primacy bias.

Intent labels are not ground-truth routing labels. Assess routing utility using
the same reply-quality rubric for both Qwen backends and the existing
always-small/always-large baselines. Preserve the frozen evaluation set and human
grades. Select prompts and thresholds on development data; use held-out examples
only for the final evaluation. A sample too small to resolve an effect should
produce an inconclusive result, not a claim that bias is absent.

## 2. Proposed routing mitigation

If provider order control is verified, randomly select either route order with
equal probability for each initial routing call. Move descriptions with their
route labels. Inject the order selector in tests so both permutations are
reproducible. Do not derive seeds from customer message text.

Keep the intent option order and `option_set_version` semantics unchanged in this
phase. Record the route presentation order separately. The 78-way intent question
may have its own ordering sensitivity, but that requires a separate experiment.

Compare three policies offline: fixed order, one randomized call, and two calls
with opposite orders. For the paired policy, accept agreement; treat disagreement
as uncertainty and evaluate escalation to the complex route under a fixed policy.
This escalation may increase cost and does not guarantee a better reply. Do not
silently choose the first answer or treat raw Jev confidence as calibrated truth.

Recommend one randomized call as the first live candidate, subject to the
evaluation gates below. Keep paired calls offline initially. Any later live
paired mode must fit the total deadline and account for both calls. The second
routing call is distinct from the post-generation quality assessment below.

## 3. Linear probe and second Jev assessment

The small Qwen model generates a reply, after which Jackson's linear probe can
estimate its trained target from activations. Agree on and document that target:
an intent-correctness probe cannot automatically certify policy adherence or the
whole reply. The probe detects likely downstream errors, not their cause in
Jev's routing decision. A small-model probe does not validate the large model.

Retain the TEAM_PLAN gate: the probe must beat the model's token-probability
confidence by development-set AUROC by November 20, 2026; otherwise it remains
an offline result. Choose the operating threshold on development data and report
false alarms and missed failures, not only AUROC.

Propose a separate Jev quality assessment after generation and the probe. Give it
the request, reply, and applicable policy context transiently. Ask whether the
reply handles the request correctly and follows policy. Hide the first routing
decision, model identity, and probe verdict from this assessment to reduce
anchoring. This is an independently prompted check, not a statistically
independent source of evidence. Audit its verdict-option order sensitivity too.

During offline development, assess all eligible replies so we can measure false
negatives. Evaluate a cheaper live policy that calls the judge only when the
validated probe flags risk, plus a predeclared sample of unflagged replies for
monitoring. If the probe fails its gate, compare judge-only checks with existing
checks rather than enabling the failed probe. Validate Jev's judgments against
human grades before letting them drive live interventions.

Sebastian's controller combines the signals under a versioned policy. A
development-validated failure signal can trigger at most one small-to-large
escalation within the remaining deadline. A timeout, unavailable probe, or
malformed judge result means unknown, not pass. Specify the fallback for each
case before rollout; if no acceptable reply is available, use the agreed failure
response. Do not create repeated judge/reroute loops. Account for extra judging
and generation latency and cost.

Changing only option order in two routing calls measures order sensitivity.
A post-generation assessment has new evidence and a different question, so it
cannot substitute for that controlled test.

## 4. Implementation sequence and owners

These are proposed contributions within the existing TEAM_PLAN ownership.

| Step | Files or interface | Owner and verification |
|---|---|---|
| Experiment | New `scripts/evaluate_jev_order.py` and synthetic fixtures | Verness with Pavlos: paired order, same-order controls, complete accounting, reproducible metadata |
| Order control | `jev.py`, `config.py`, env templates, Jev client tests | Verness: preserve route/description mapping, exercise both orders, keep intent options unchanged |
| Assessment records | `router.py`, `records.py`, `store.py`, new migration if needed | Verness with Sebastian: preserve distinct routing and quality-check records and persist them consistently |
| Probe signal | Activation/probe interface with Qwen backend | Jackson with Yanzhen: versioned target, model compatibility, dev AUROC gate and threshold |
| Quality assessment and recovery | Separate Jev judge interface, `service.py`, `monitor.py` | Verness with Sebastian: blinded assessment, unknown handling, bounded escalation and deadline tests |
| Evaluation and rollout | Harness, metrics, architecture YAML/SVG when implemented | Pavlos with Yanzhen and Sebastian: baselines, quality/cost comparison, reviewed rollout criteria |

Record request or synthetic example ID, call stage, order, canonical choice,
validity/error code, model/prompt versions, probe version/score when applicable,
controller action, token usage, and latency. Never persist customer message text,
raw prompts, raw provider bodies, or free-form judge explanations that may echo
the message. Keep request IDs out of Prometheus labels. Mock tests verify
mechanics; only live controlled measurements establish provider behavior.

## 5. Acceptance criteria and rollback

Before examining held-out results, agree on numerical quality non-inferiority,
p95 latency, and cost limits, plus the order-effect reduction needed to justify
the selected mitigation. Those limits are review decisions, not measured values
in this proposal. Check critical intent groups as well as aggregate results.

Live enablement requires verified order control, correct label mapping, a
reported comparison against fixed-order and always-small/large baselines, and
acceptable quality and operational results under those predeclared limits.
Randomization alone must not be described as eliminating order sensitivity.
Probe-driven actions additionally require the existing probe gate; judge-driven
actions require human-grade validation. If evidence is inconclusive, retain the
current policy and report the experiment offline.

Implement separate Pydantic Settings switches for randomized routing and
post-generation checks, defaulting off until validation. Roll back by disabling
the affected feature and restoring the previously validated policy. Preserve
metadata for diagnosis without customer text. Leave applied migrations intact;
make schema changes through new migrations. Verify rollback with mocked errors,
timeouts, unavailable probes, and controller disagreement before rollout.

## Research basis

- [Large Language Models Sensitivity to The Order of Options in Multiple-Choice Questions](https://aclanthology.org/2024.findings-naacl.130/)
  motivates controlled option-order comparisons.
- [Large Language Models are not Fair Evaluators](https://arxiv.org/abs/2305.17926)
  motivates balanced position comparisons for model judgments.

These studies motivate the experiment; they do not establish results for Jev or
our gateway.
