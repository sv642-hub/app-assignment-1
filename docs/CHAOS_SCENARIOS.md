# Chaos scenarios

The list of faults the gateway should survive, what it should do in each, and what to measure. It turns the diagram's "About 20 scenarios; detection and recovery times over many runs; false interventions" into a plan.

**Status: groups A to E are built and measured once on a local stack with the mock models (C ten times); see [Measured on November 27](#measured-on-november-27-2026).** `scripts/measure_recovery.py` runs each of A1–E4 by name (`--scenario D4`, or `--group C`); `scripts/chaos_demo.py` still runs the three original faults once each, for the demo. F, G and H are still a plan.

Owner: Pavlos (scenarios and fault hooks). The other owners are named where a scenario touches their area.

## What the gateway does today

This is from the code on `main` at `1ed9c0b`. Each "Expected" below follows from these rules.

- **Detection.** The controller keeps each model's last 20 calls (`APP_MONITOR_WINDOW`). It disables a model when at least half of them failed (`APP_MONITOR_FAILURE_THRESHOLD=0.5`), checked from the 5th call on (`APP_MONITOR_MIN_SAMPLES=5`).
  - A failure is a failed format check or a call that raised.
  - Wrong intents count only through the semantic monitor, which acts only with `APP_SEMANTIC_ACTIONS_ENABLED`.
- **Recovery.** A disabled model comes back 60 s later (`APP_CONTROLLER_COOLDOWN_SECONDS`), on the first request after that, with an empty window.
- **Never the last model.** A model is disabled only if the other one is usable. Otherwise the controller logs `kept_last_backend` and keeps both.
- **Customers see these differently:**
  - **A call that raises** is retried on the other model, so the customer still gets a good reply, just slower (`selected_backend_error`).
  - **A reply that fails the format check is not retried.** The customer gets the apology until the model is disabled.
  - **When neither model answers,** the gateway returns 503.
- **Deadline.** Each request has 45 s (`APP_REQUEST_DEADLINE_SECONDS`). The first model gets that time minus 15 s, which is kept back for the second (`APP_ALTERNATE_RESERVE_SECONDS`).
- **Jev.** Each call has a 2 s timeout (`APP_JEV_TIMEOUT_SECONDS`). After 5 failures in a row, the breaker opens for 30 s, and the rule router takes over.
- **Store.** If Postgres is down, the customer still gets the reply, but it isn't saved. Each save waits up to 2 s (`APP_STORE_TIMEOUT_SECONDS`), and saves queue behind each other.
- **Memory.** Monitor and controller state live in the gateway's memory, so a gateway restart forgets every window and disable.

## What every run records

For each scenario and each run:
- **Detection:** time and requests from the fault to the disable, and bad replies customers saw before it.
- **Recovery:** time from clearing the fault to the model answering again.
- **503s:** how many requests got one.
- **False interventions:** disables with no fault, or a disable of the model that wasn't faulted.
- **Latency:** p50 before the fault, during it, and after the disable.

Repeat each scenario enough times to give a rate, not one example. At least 10 runs where the outcome is random: partial faults and anything near the threshold.

## The scenarios

**Hook** says what's needed to inject the fault:
- **existing:** `/v1/faults` with a fault kind, a rate and a model, or a plain Docker command
- **new:** a fault hook to add
- **real:** needs the gateway calling vLLM

### A. Baselines

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| A1 | Healthy | Nothing, mixed traffic for 10 minutes | No interventions at all. Any disable is a false intervention | existing |
| A2 | Healthy after a disable | A1 straight after a scenario that disabled a model | No new disable once the model is back: the window starts empty | existing |

### B. One model, the full fault (the three built so far)

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| B1 | Small model down | `outage`, rate 1, small | Disabled after 5 calls. Customers see no apology; replies come from the large model and are slower | existing |
| B2 | Small model malformed | `bad_format`, rate 1, small | Disabled after 5 calls. Customers see the apology on those 5 | existing |
| B3 | Small model wrong intents | `label_swap`, rate 1, small, semantic actions on | Disabled by the semantic monitor after about 10 replies. Customers see wrong answers until then | existing |
| B4 | Large model down | `outage`, rate 1, large, complex traffic | Like B1 the other way round: the large model is disabled and the small one answers complex messages | existing |
| B5 | Large model malformed | `bad_format`, rate 1, large, complex traffic | Like B2 the other way round | existing |

### C. Partial faults: where the threshold matters

The controller checks from the 5th call, when the window holds only 5 calls. So a fault below 50% can still trip it early. The chance that 3 or more of the first 5 calls fail is about 1% at a 10% fault rate, 16% at 30%, and 50% at 50% (binomial). Over a longer run, the chance of a disable is higher than that.

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| C1 | Light | `bad_format`, rate 0.1, small | Rarely disabled | existing |
| C2 | Below threshold | `bad_format`, rate 0.3, small | Usually not disabled, but sometimes, often in the first calls. **Measure how often.** It decides whether `APP_MONITOR_MIN_SAMPLES=5` is too low | existing |
| C3 | At threshold | `bad_format`, rate 0.5, small | Disabled in most runs, after a varying number of calls | existing |
| C4 | Above threshold | `bad_format`, rate 0.7, small | Disabled in nearly every run, a little later than B2 | existing |
| C5 | Partial outage | `outage`, rate 0.3, small | Like C2 in how often it trips, but customers see no apology: each failed call is retried on the large model | existing |

### D. Both models, and the last model

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| D1 | Both down | `outage`, rate 1, both | Every request gets a 503. The first model to trip is disabled, then the other is kept (`kept_last_backend`). Both back once the faults clear | existing |
| D2 | Both malformed | `bad_format`, rate 1, both | No 503s, but every reply is the apology. One model disabled, the other kept | existing |
| D3 | One down, one malformed | `outage` on small, `bad_format` on large | Customers get apologies from the large model, since it's the only one left. The malformed large model can trip first and be disabled; when the dead small model then trips with nobody else left, the controller brings the large one back and disables the small one (a 503 for a few requests, then apologies). Checked in process by the D3 test; also run on a local stack, see the D3 note under the measurements | existing |
| D4 | Operator hold, then a fault | Operator disables the large model, then `outage` on small | The small model is never disabled, because it's the last usable one, so every request gets a 503. Checks that an operator hold counts as "not usable" | existing |

### E. Timing and flapping

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| E1 | Fault returns after recovery | B2, clear it, then the same fault again right after the re-enable | Disabled again after 5 more calls, because the window was reset | existing |
| E2 | Fault cleared during the cooldown | B1, cleared 10 s into the 60 s cooldown | The model stays out for the full cooldown: there's no early return. Recovery is about the cooldown, not the fault | existing |
| E3 | Flapping | `bad_format` on for 10 s, off for 10 s, over 5 minutes | Count disables and how much of the time each model was out. Shows whether the controller keeps up or lags | existing |
| E4 | Fault with no traffic | `outage` on small, only complex messages for 2 minutes, then standard ones | No disable while no standard traffic reaches the small model. Detection starts with the first standard message | existing |

### F. Parts around the models

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| F1 | Postgres down | `docker compose stop postgres` for 1 minute | Customers still get replies, but none are saved. Latency rises by up to 2 s per save, more as saves queue. History is back when Postgres is | existing |
| F2 | Gateway restart during a disable | B2, then `docker compose restart gateway` during the cooldown | The disable is forgotten, and the faulty model is used again at once. **A known gap**, because monitor state is in memory; the diagram already says so | existing |
| F3 | Prometheus down | `docker compose stop prometheus` | No effect on customers. Only the metrics have a gap | existing |
| F4 | Jev down | Jev calls fail or time out | The breaker opens after 5 failures, and the rule router answers. Closes after 30 s. Verness owns the router | new |
| F5 | Jev slow | Jev answers after 2.5 s | Every call hits the 2 s timeout, so the same as F4, but each request waits 2 s first | new |

### G. New fault hooks

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| G1 | Slow model | Small model replies after 20 s | Answers, inside the 30 s first-model budget. Latency only; no disable, because slow isn't a failure | new |
| G2 | Hung model | Small model replies after 40 s | Cut off at 30 s, then the large model gets the remaining 15 s. Counts as a failed call | new |
| G3 | Bad prompt | Every reply is a well-formed `unknown` | Passes the format check, so the failure monitor sees nothing. The semantic monitor sees it disagree with Jev. This is the diagram's "Bad prompt" todo | new |

### H. Real models (once the gateway calls vLLM)

These are the faults that matter in deployment, and they belong to the GPU host.

| # | Scenario | Inject | Expected | Hook |
|---|---|---|---|---|
| H1 | One vLLM server stopped | `docker compose ... stop vllm-small` on the GPU host | Like B1, with real latencies | real |
| H2 | GPU host down | Shelve the GPU instance, or stop both servers | Every request gets a 503, as in the diagram's red note. Back once the start-at-boot unit has both servers up, about 3 minutes | real |
| H3 | Wrong API key | A wrong `APP_VLLM_API_KEY` on the gateway | Every call gets a 401 and fails, so the same as H2. Shows a config mistake looks like an outage | real |
| H4 | One server restarts while the other runs | Restart `vllm-large` alone | It may fail its memory check, because the margin is about 0.02 GiB, and stay down. The real version of D1's half. Fix: restart both together | real |
| H5 | Real format failures | None: run normal traffic | A first 50-message check, without #112's settings, saw some real format failures. Not measured properly yet. Measure on the full test set, then see whether the controller's 50% threshold ever trips on its own | real |

That's 33 scenarios. 23 need only what exists today, 5 need a new fault hook, and 5 need the real models.

## Order

1. ~~After #104 merges: add A, B4–B5, C, D and E to its script as named scenarios.~~ Done, and run once on a local stack (C ten times). More runs of C2 and C5 would narrow their rates.
2. **F1–F3** are Docker commands around the same measurement loop.
3. **New hooks for G1–G3**, in `MockBackend` and `/v1/faults`. These are Pavlos's. F4–F5 need a fault in the Jev mock, so check with Verness first.
4. **H**, once Verness has the gateway calling vLLM.

## Measured on November 27, 2026

Local Compose stack on a Mac, mock models and mock Jev, `compose.measure.yml` (semantic actions on, baseline 0), default controller settings (window 20, 5 samples minimum, 50% threshold, 60 s cooldown), one message every 0.5 s. Groups A, B, D and E ran once each and every run showed what its scenario expects; group C ran 10 times per scenario. "Requests" counts the requests on the faulted model's route, from the fault to the disable. These are mocks: the numbers show how the controller's rules play out, not how real models behave.

| # | Disabled | Requests | Time to disable | Bad replies before it | 503s | Back after the clear | Notes |
|---|---|---|---|---|---|---|---|
| A1 | none | | | 0 | 0 | | 1,199 healthy messages in 10 minutes, no intervention |
| A2 | small | 6 | 3.2 s | 6 | 0 | 58.1 s | Then 60 healthy messages, no intervention |
| B1 | small | 6 | 3.5 s | 0 | 0 | 58.2 s | The large model answered every failed call |
| B2 | small | 6 | 3.2 s | 6 | 0 | 57.8 s | |
| B3 | small | 4 | 2.2 s | 4 | 0 | 58.3 s | Semantic monitor |
| B4 | large | 6 | 3.5 s | 0 | 0 | 58.5 s | The small model answered every failed call |
| B5 | large | 6 | 3.4 s | 6 | 0 | 58.7 s | |
| D1 | large first | 3 | 1.5 s | 3 | 13 of 13 | 55.8 s | Small kept as the last model; every request a 503 |
| D2 | small first | 7 | 3.2 s | 7 | 0 | 55.0 s | Large kept; 17 apologies, no 503s |
| D3 | large first | 3 | 1.5 s | 3 | 10 of 13 | 55.7 s | Small (down) kept. Nov 27 run, before the swap; see below |
| D4 | none during the hold | | | 20 | 20 of 20 | | Small kept; then disabled 0.1 s after the hold was released, see below |
| E1 | small, twice | 6, then 4 | 3.2 s | 6 | 0 | 58.1 s | |
| E2 | small | 6 | 3.5 s | 0 | 0 | 48.0 s | Cleared 10 s into the cooldown: back only when it ended |
| E3 | small, 5 times | 6 | 3.2 s | 33 in all | 0 | 28.8 s | Out 90% of the 5 minutes of flapping (272 of 302 s) |
| E4 | small | 6 | 124.3 s | 0 | 0 | 58.0 s | No disable in the 2 minutes with no standard traffic; then 6 requests |

Group C, 10 runs each, a fixed 60 messages (45 to the small model):

| # | Fault | Runs with a disable | Median requests to it | Fault rate the mock drew, until any disable |
|---|---|---|---|---|
| C1 | `bad_format` 10% | 0 of 10 | | 9.8% |
| C2 | `bad_format` 30% | 5 of 10 | 30 (19 to 41) | 35% |
| C3 | `bad_format` 50% | 10 of 10 | 28 (16 to 44) | 47% |
| C4 | `bad_format` 70% | 10 of 10 | 12 (6 to 17) | 73% |
| C5 | `outage` 30% | 5 of 10 | 40 (33 to 45) | 31% |

How to read them:
- **"Disabled after 5 calls" holds only from an empty window.** Each run sends 8 healthy messages first, which leaves 6 good calls on the faulted route, so it takes 6 failures. E1's second disable takes 4: the reply that showed the model was back is a good call in the fresh window, and 1 good plus 4 failures reaches the 5-call minimum at 80%.
- **C2 and C5 answer the open question on `APP_MONITOR_MIN_SAMPLES`.** At 30% faults, every disable came after 19 or more requests, once the 20-call window was full, never in the first calls. So raising the minimum to 10 would not change them; the window, the threshold and how long the fault lasts do. Replaying each run's failures through the rule gave the same disable, at the same request, in all 20 runs. The runs that tripped drew more faults than 30%: 43–53% in 4 of the 5 C2 runs, and 33–39% in the C5 runs. A simulation of the rule at exactly 30% gives about 22% of 45-call runs; 5 of 10 runs is too few to tell the two apart.
- **D1: which model goes first depends on the traffic, not the fault.** Both models fail every request, but mixed traffic left the large model only 2 good calls, against 6 for the small one, so it reached 50% first. The request that disabled it still ended in a 503, which has no trace; the script found the disable in the intervention log.
- **D3: the controller swaps the dead model out.** The first run (Nov 27) kept the dead small model as the last one: the large model's window filled twice as fast, because every failed small call is retried on it, so it was disabled first and 10 of 13 requests got a 503. The controller now swaps: when the dead small model reaches the threshold as the last model, it brings the large model back and disables the small one. On a local stack (Nov 28, review of #149) that gave 2 of 13 503s, then apologies from the large model.
- **D4: releasing the hold disables the small model with its fault gone.** During the hold the small model is kept as the last usable one and every request gets a 503, as expected. After the fault clears and the hold is released, the small model's window is still full of outage failures, so the next call to it disables it (0.1 s after the release), and it stays out for the full cooldown while the large model answers. Whether the window should be cleared when the other model becomes usable again is the monitor's call (Sebastian). #144 clears the kept model's window when the other model is usable again, so this no longer happens; the D4 run in this file predates that change.
- **E2 and E3: recovery is about the cooldown.** Clearing a fault early doesn't bring the model back sooner. With a fault flapping every 10 s, the model is disabled again about 3 s after each return while the fault is on, so it was out 90% of the time.

The tests in `tests/test_measure_recovery.py` check the same rules in process, with a 1 s cooldown.

## Open questions

- **C2:** ~~should `APP_MONITOR_MIN_SAMPLES` rise to 10?~~ Measured: the 30% trips all came with a full window, so the minimum is not what causes them. If 30% faults should never disable a model, that needs a longer window or a higher threshold. Sebastian owns the monitor.
- **D4:** see the note under the measurements. It is about which model the controller keeps; Sebastian's call.
- **F2:** losing state on restart is known. Is it acceptable for this release, or should disables be saved to Postgres and read back at start? It's the gateway's and the monitor's call.
- **H5:** real format failures may be common enough to matter more than any injected fault. Decide after the full real-model run.
