# Team plan: Smart AI Gateway

Final project for DSAN 6700. This file records who owns what. Owners decide and review changes in their area; anyone can contribute anywhere.

## Owners

| Person | Owns |
|---|---|
| Verness Chin | Gateway, real Jev client, routing and rule-router fallback; Deployments and Jev screens |
| Pavlos Baffes | GPU host, vLLM containers, Docker Compose, ports and secrets; evaluation harness runner and chaos scenarios |
| Yanzhen Gao | Qwen backends: prompt (classify, read policy card, reply), constrained JSON output, vLLM client with timeouts; per-model accuracy and the always-small / always-large baselines |
| Sebastian Villalobos | Semantic monitor and controller (reroute, fall back, roll back); Prometheus and Postgres; integration across areas; documentation |
| Jackson Howes | Linear probe on Qwen3-1.7B activations; Chaos mode screen and probe results view; judge validation if time allows |
| Eric | Routing analysis: the Banking77 development split; the Jev route-order (position bias) experiment; the routing threshold's quality and cost trade-off against the always-small, always-large and oracle baselines |
| Everyone | 60 evaluation messages and 30 human reply grades each |

## Interfaces to agree first

- **Yanzhen + Pavlos:** vLLM URLs for both models and the request/response format. Yanzhen builds against a local stub until the GPU host is ready.
- **Verness + Sebastian:** the Jev assessment records (one combined routing and intent call per request) that the monitor reads.
- **Yanzhen + Sebastian:** the backend response record (intent, reply, format check, latency, version ID) that the monitor scores.
- **Eric + Pavlos + Yanzhen:** the Banking77 development split that the harness, the baselines, the semantic baseline and the probe threshold use. The frozen evaluation set stays separate.
- **Eric + Verness:** the route-order experiment: how it calls the real Jev client, and what each call records (order shown, answer by route name, errors, tokens), with no message text.

## Evaluation set (shared)

- 300 fresh customer messages, 60 per person. Each person writes about one message per intent across all intents, not a block of intents, so no intent carries one writer's style.
- About 20% hard on purpose: close to a neighboring intent, very short, or very long.
- No personal data, no copied Banking77 sentences, one intent per message.
- Shared sheet columns: `text`, `intent`, `writer`, `difficulty`, `checked_by`.
- Every batch is labeled again by a different person without seeing the writer's label. Disagreements are settled once; messages nobody agrees on are dropped.
- The set is frozen on a fixed date, before any prompt or threshold tuning, and does not change after that.

## Human grades (shared)

- 150 reply grades, 30 per person, about 20 graded by two people so we can report how often graders agree.
- These grades are what the judge model is checked against.

## Probe gate

The probe must beat the model's own token-probability confidence (AUROC on the dev set) within **one week after both of these are ready**:

- Qwen3-1.7B runs on the GPU host, so the probe can read real activations.
- The Banking77 development split exists.

If it does not, it is reported as an offline result and stays out of the live system. The earlier date, Friday, November 20, 2026, was moved because the GPU host is not deployed yet, so the probe cannot be tested.

## Diagrams

- **Architecture status:** `docs/architecture.svg`, generated from `docs/architecture.yaml`. Every PR that changes what is built, simulated or owned updates the YAML (see `AGENTS.md`). Sebastian keeps the layout and reviews these changes.
- The proposal's architecture diagrams still show Streamlit. Update the frontend box to "React (Vite), static build served by Caddy" before the final deliverable.
