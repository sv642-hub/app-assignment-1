# Team plan: Smart AI Gateway

Final project for DSAN 6700. This file records who owns what. Owners decide and review changes in their area; anyone can contribute anywhere.

## Owners

| Person | Owns |
|---|---|
| Verness Chin | Gateway, real Jev client, routing and rule-router fallback; Deployments and Jev screens |
| Pavlos Baffes | GPU host, vLLM containers, Docker Compose, ports and secrets; evaluation harness runner and chaos scenarios |
| Yanzhen Gao | Qwen backends: prompt (classify, read policy card, reply), constrained JSON output, vLLM client with timeouts; per-model accuracy and the always-small / always-large baselines |
| Sebastian Villalobos | Semantic monitor and controller (reroute, fall back, roll back); integration across areas; documentation |
| Jackson Howes | Linear probe on Qwen3-1.7B activations; Chaos mode screen and probe results view; judge validation if time allows |
| Everyone | 60 evaluation messages and 30 human reply grades each |

## Interfaces to agree first

- **Yanzhen + Pavlos:** vLLM URLs for both models and the request/response format. Yanzhen builds against a local stub until the GPU host is ready.
- **Verness + Sebastian:** the Jev assessment records (one combined routing and intent call per request) that the monitor reads.
- **Yanzhen + Sebastian:** the backend response record (intent, reply, format check, latency, version ID) that the monitor scores.

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

The probe must beat the model's own token-probability confidence (AUROC on the dev set) by **[PROBE GATE DATE]**. If it does not, it is reported as an offline result and stays out of the live system.

## Diagrams

The architecture diagrams still show Streamlit. Update the frontend box to "React (Vite), static build served by Caddy" before the final deliverable.
