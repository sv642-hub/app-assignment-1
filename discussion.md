---
title: "Smart AI Gateway: discussion"
---

**Team:** Sebastian Villalobos, Verness Chin, Pavlos Baffes, Yanzhen Gao, Jackson Howes, Eric Kim. DSAN 6700, final project.

## User and problem

The user is a bank's support team that wants customers to get a correct, approved answer to a banking question quickly, without paying for the largest model on every message. A small model is fast and cheap but errs more; a large model is better but slower and costlier. Our gateway reads each message, asks Jev (a TypeSafe model) to pick a route (standard or complex) and an intent, and sends the message to a small (Qwen3-1.7B) or large (Qwen3-4B) model. The intent set is the 77 Banking77 intents (Casanueva et al., 2020) plus `unknown`. We built the system around a second problem: a model in production can fail quietly, by hanging, returning malformed output or answering the wrong intent. The gateway therefore watches its own models and recovers without a person. This is a showcase of that workflow, not a general chatbot: replies come from ten approved policy cards of a fictional bank, and anything else gets a fixed no-match reply.

## Architecture

A message goes through Caddy to a FastAPI gateway, which calls Jev (or a rule router if Jev is slow or down), then one of two vLLM servers on a separate GPU instance, checks the reply's format, builds the reply from the policy card and saves a record to Postgres. A monitor scores each model's recent calls; a controller disables a model that fails and moves its traffic to the other. Prometheus and Grafana show the result. Figures are in `docs/ARCHITECTURE.md` (process, execution, state and technical views, with every time limit) and `docs/architecture.svg` (what is built, simulated or planned). Two decisions shaped it. First, every external part sits behind an interface with a mock (Jev, both models), so the monitor and controller were built and tested before the GPU host existed. Second, the model's reply text is discarded and the reply is built from the card, so a model failure can change which approved answer appears but never what an answer says. State is kept small: the customer's message is never stored, the monitor's windows are rebuilt from Postgres at startup, and the gateway runs as one process (`docs/KUBERNETES.md` explains what two copies would break).

## Baseline, improvements and how success was measured

**Baselines.** The comparison points are always-small, always-large and the oracle, which is the best any router over these two models could do (the large model only where it is right and the small one is wrong). They are computed by `scripts/run_eval.py` on the official Banking77 test set (3,080 messages), scoring accuracy and macro F1 with a 95% Wilson interval. We do not have accuracy or macro F1 for the real models yet, so this report states none; the runner and the oracle calculation are built and tested, and on the mock models they only check that the runner works. A full rerun on the real models found format failures of 0.8% for Qwen3-1.7B and 0.03% for Qwen3-4B, against the controller's threshold of 0.5.

**Improvements.** (1) *Replies from cards.* Real Qwen added facts or text not in the card; replies are now built from the card and uncarded facts are dropped. (2) *Hung-model detection.* One run took 186 s to disable a hung model; a 6 s per-call limit and a streak rule (3 timeouts) now target about 18 s (see below). (3) *Per-model rollback.* A new model release that fails while watched rolls itself back instead of disabling the model. (4) *Escalation and the reply check* (the large model answers when the small one is unsure; a second Jev call checks the reply) exist but are off by default, because their thresholds wait on development data. We state them as built, not as improvements, since we have not measured that they help.

**How success was measured.** We kept the targets in one catalog (`config/metrics.yaml`) that tests compare against the dashboard, alerts and docs. Availability is 99% of requests answered (no 503); p95 latency is under 2 s (small) and 4 s (large); a faulty model should be disabled within 30 s. These are proposals, not agreed with a real customer (`docs/SLO.md`). We measured recovery by injecting five faults through `/v1/faults` and timing the controller. On real Qwen, from the fault being injected to the model being disabled: outage 12.7 s, bad format 12.4 s, bad output 12.7 s, label swap 8.5 s, each over four to six requests, with no 503s and no false interventions, and each model serving again after about 50 s. The hang took **185.9 s**, and customers routed to it waited about 31 s (normal: 0.42 s). This is one run per scenario. The 18 s expected after the fix has **not** been measured on real Qwen.

## Homework 3 tester findings and what we did

Testers filed issues, and we sorted them into fixed, stated limit and open. *Fixed* (#108): the draft and conversation vanished on refresh, so the browser tab now keeps them; a character counter, a short explanation of confidence and a topics list were added. *Stated limits, closed:* no login (#36), because the server stores no message and each message stands alone, so there is no per-user data to separate (`docs/SECURITY.md`); plain HTTP (#37), which which is being replaced with HTTPS on ports 80 and 443. *Open:* a message with two questions answers only one (#38). Banking77 has one label per message, so the system answers one intent per message, so for now we ask for two messages. Reply feedback (#41) needs a new table and a migration. The tester brief now says what is simulated and what is real.

## Decisions and lessons

The most useful decision was to build the monitoring loop against mocks first, then run each fault against the real thing. That order found the hang problem: the hang scenario had never been run on a real model, and there it was the slowest fault by a factor of fifteen (`docs/POSTMORTEM.md`). The cause was a product of three reasonable settings, not a bug: a 30 s first-attempt budget kept for the fallback, a controller that waits for five finished calls, and a monitor that only counts finished calls. The lesson is that a monitor must not rely on the thing it monitors to finish.

Other decisions were deliberate. We left semantic actions (disabling a model because its intent disagrees with Jev) off by default until the disagreement baseline comes from development data, because a wrong baseline would disable healthy models. We kept testers on real Jev, accepting a paid call per message, so their feedback matched the system we were describing. We chose a single gateway process and wrote down what that costs. We closed login and HTTPS as limits at first and stated them to testers; HTTPS is now being added because the deliverable requires it. What we would do differently: run the hang scenario on real models in the first week, set the 6 s limit from tester load instead of one evaluation run, and agree the evaluation set and grading before building the monitors. The linear probe (Jackson) is a stretch goal that has not been run on real activations, so we report no probe result.

## References

Casanueva, I., Temčinas, T., Gerz, D., Henderson, M., and Vulić, I. (2020). Efficient intent detection with dual sentence encoders. *Proceedings of the 2nd Workshop on NLP for Conversational AI*, 38–45.

Kwon, W., Li, Z., Zhuang, S., et al. (2023). Efficient memory management for large language model serving with PagedAttention. *Proceedings of SOSP 2023*.

Yang, A., et al. (2025). Qwen3 technical report. arXiv:2505.09388.
