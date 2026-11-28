# Postmortem: a hung model took 186 seconds to be disabled

Blameless. It describes what the system did, not who was at fault. Status: **fixed in #135, fix not yet measured on real Qwen.**

## Summary

During the real-Qwen end-to-end run on 2026-11-26, we made the small model (Qwen3-1.7B) hang on every call. Nobody got a wrong reply or a 503, because the large model answered each request after the small one gave up. But each customer routed to the small model waited about 31 seconds, and the controller needed about 186 seconds to disable the hung model. A healthy request takes about 0.4 to 0.8 seconds. The detection time missed the draft target of 30 seconds by a factor of six.

The cause was not a bug. Three settings, each reasonable alone, combined badly: a 30 second first-attempt budget, a controller that waited for 5 finished calls, and a failure rate that counted only calls that had finished. #135 changed the first and second.

## Impact

Measured in one run on a throwaway branch (`main` plus several open PRs) against real Qwen on the GPU instance, with the fault injected through `POST /v1/faults`. The raw results are in the project files as `real-qwen-e2e-results-20261126T033836-5f48.zip` (`real_faults.json`). These are single runs, not averages.

| What | Hang run | Other faults, same run (outage, bad format, bad output, label swap) |
| --- | --- | --- |
| Time from the fault being injected to the controller disabling the model | **185.9 s** | 8.5 to 12.7 s |
| Requests that reached the broken model before it was disabled | 6 | 4 to 6 |
| Customer latency while the fault was active | **30.6 s** per hung request (normal: 0.42 s) | 0.43 to 0.69 s |
| Replies that were wrong, or 503s | 0 and 0 | 0 to 6 wrong replies, 0 503s |
| Time until the model served again after the cooldown | 52.4 s | 50.4 to 54.4 s |
| False interventions | 0 | 0 |

So customers were protected from bad content, and the system recovered on its own, but the slow path was slow for about three minutes. The pages that fired were late too: `ModelFailureRateRising` after 93 s, `ChatLatencyHigh` after 156 s and `ModelDisabledByController` after 215 s, all counted from the run's start (the fault began at 16 s).

Only customers whose message was routed to the small model were affected. Customers routed to the large model saw nothing.

## Timeline of the run

Times are seconds from the start of the run. The fault was injected at 16 s.

| Time | What happened |
| --- | --- |
| 16 s | The small model starts hanging. The first request routed to it waits |
| 46 s | The first hung call hits the first-attempt limit (30 s) and is cut off. The large model answers in under a second. Total for that customer: about 30.6 s |
| about 50 to 200 s | The pattern repeats for each request routed to the small model. Each one adds a failed call to the monitor window, but only when it finishes, 30 s later |
| about 93 s | `ModelFailureRateRising` goes pending (the window crossed the early-warning rate) |
| about 156 s | `ChatLatencyHigh` goes pending: the 30 s calls now show up in the request latency |
| 202 s | The sixth failed call finishes. The controller has the 5 samples it needs and a failure rate above 50%, and disables the model (`detect_seconds` 185.85, measured from the moment the fault was injected) |
| 210 s | The fault is cleared by the test |
| after 60 s cooldown | The model is re-enabled with an empty window, and serves again |

## Root cause

A hung call is the slowest possible failure, and our detection counted failures only after they finished.

1. **A 30 second budget for the first attempt.** A request gets 45 s in all (`APP_REQUEST_DEADLINE_SECONDS`). The first attempt gets that minus 15 s kept in reserve for the other model (`APP_ALTERNATE_RESERVE_SECONDS`). That reserve was right, because it is why customers still got answers. But it also meant a hung call cost the customer the full 30 s.
2. **The controller waited for 5 finished calls** (`APP_MONITOR_MIN_SAMPLES`). That rule exists so one failure in two calls does not disable a model. With a 30 s call, 5 samples take at least 150 s.
3. **The monitor only saw finished calls.** A call still waiting for 29 s was invisible to it. The in-flight gauge showed it, but nothing acted on that.

The other four faults finish fast (an error returns at once, a bad reply comes back quickly), so 5 samples took 4 to 6 requests and 8 to 13 s. The hang was the only fault where the cost of one sample was 30 s.

## Why we did not catch it sooner

- The hang scenario (G2 in CHAOS_SCENARIOS.md) was still marked `new`: nobody had run it against a real model before this run. The other faults had already been measured on the mocks.
- The README, MONITORING.md and METRICS.md gave 186 s as an estimate (5 requests at about 31 s each) and say it was not a measured run. The run gave the same number, so those pages should now cite it (see Follow-ups).
- The draft target of 30 s for detecting a fault was written before anyone had run a hang on real Qwen.

## What went well

- No customer got a wrong reply or a 503 while the model hung. The other model answered every request.
- The controller disabled the right model, left the other one alone, and re-enabled it after the cooldown. There were no false interventions.
- The failing case was found in a planned end-to-end run, before the final demo, not by a customer.

## What we changed

[#135](https://github.com/sv642-hub/app-assignment-1/pull/135), merged:

- **Per-call limit.** `APP_ATTEMPT_TIMEOUT_SECONDS` (default 6): while the other model could still answer, one call gets at most 6 s. Real medians are 0.37 s (small) and 0.85 s (large), so 6 s is several times the slowest healthy reply. The 6 s limit rests on one eval run's latencies, not on tester load.
- **Streak rule.** `APP_MONITOR_TIMEOUT_STREAK` (default 3): three timeouts in a row disable the model at once, without waiting for 5 samples. Any reply or other error breaks the streak.
- **A measurement.** `gateway_fault_detection_seconds` records the time from a model's first failure to its disable, by model and reason, so the next run reports it directly.

The expected worst case is now 3 x 6 s = about 18 s to disable, and 6 s per affected customer instead of 31 s. **This has not been measured on real Qwen.** The real rerun has not happened.

## Trade-offs we accepted

- A model that is only slow, past 6 s on every call, is treated like a hung one and disabled after 3 in a row. That is intended, but a busy GPU could trigger it. The setting is an environment variable.
- A hung model still costs the first customers up to 6 s each, three times, before it is disabled. Detecting it before the first timeout would need a health probe, which we did not build.
- The 45 s deadline and 15 s reserve are unchanged. If both models hang, a customer waits until the deadline and gets a 503 (`deadline_exceeded`).

## Follow-ups

| Action | Owner | Status |
| --- | --- | --- |
| Rerun the hang scenario on real Qwen with #135 and record `detect_seconds` (expected about 18 s, target under 30 s) | Sebastian | Open |
| Update the README, MONITORING.md and METRICS.md wording from "estimate" to cite the Nov 26 run, and later the rerun | Sebastian | Open |
| Decide whether the 6 s limit holds under tester load (watch `gateway_chat_answer_latency_seconds` for the small model) | Sebastian | Open |
| Consider acting on the in-flight gauge, so a stuck call counts before it finishes | Sebastian | Not planned |

## What we learned

- The failure that matters most for a monitor is the one that makes its own measurements slow. Rates over finished calls say nothing while the calls are still stuck.
- A safety margin (the 15 s reserve) and a detection delay (5 samples) each looked fine alone. The incident came from multiplying them.
- Run each fault once against the real thing before trusting a mock's numbers.
