# Service level objective: `POST /v1/chat`

The main route is the one customers use: they send a banking message and get a reply. This page says what "working" means for it, how we measure that, and what the numbers look like so far. The values are **proposals**: we have set them from a handful of runs, not from months of traffic, and nobody has agreed them with a real customer.

The definitions and queries live in [`config/metrics.yaml`](../config/metrics.yaml) (see [METRICS.md](METRICS.md)); a test fails when the dashboard, the alerts and the docs drift from it. This page explains the choices.

## The objectives

| | Objective | Measured by | Status |
| --- | --- | --- | --- |
| **Availability** | 99% of chat requests are answered without a 503 | `chat_availability` (`1 - chat_unavailable_ratio`) | Proposal |
| **Latency, small model** | p95 under 2 s, for answered requests | `answer_latency_p95_seconds`, model `qwen3-1.7b` | Proposal |
| **Latency, large model** | p95 under 4 s, for answered requests | same, model `qwen3-4b` | Proposal |
| **Hard cap** | No request runs longer than 45 s | `APP_REQUEST_DEADLINE_SECONDS`; the request ends with 503 `deadline_exceeded` | Enforced by the code |
| **Time to disable a broken model** | Under 30 s from its first failure | `gateway_fault_detection_seconds` | Draft target, see below |

"Answered" means HTTP 200. A reply that is wrong but well-formed still counts as available: correctness is a different question, measured offline (see the evaluation harness) and online by the semantic monitor.

## Why these numbers

- **99%, not 99.9%.** The service has one gateway process, one GPU instance and one CPU instance on a university allocation, and no failover for the GPU host. If it fails, both models fail and the gateway returns 503. 99% allows about 7 hours of failure in 30 days. A higher target would promise something the design cannot keep.
- **2 s and 4 s.** Measured medians on real Qwen are 0.37 s (small) and 0.85 s (large) from one evaluation run. The targets are several times the median, so one slow call does not break the objective, and a customer typing a banking question will wait that long. The large model gets more because it is slower and handles the complex route.
- **30 s to disable a model.** A draft number, set before any hang had been run on real Qwen. Until the model is disabled, each request routed to it pays for the failure; the target keeps that to a few requests.
- **p95, not average.** The failures we built the system for (a hang, a slow model) show up in the tail first.

## The error budget

The budget is the share of requests allowed to fail: 1% of requests. `error_budget_burn_rate` divides the 503 share by that allowance: 1 spends the budget at exactly the planned pace; above 1 spends it faster. The dashboard shows it over the window you pick, not over 30 days.

Two alerts guard the objective:

| Alert | Fires when | What to do |
| --- | --- | --- |
| `ChatMostlyFailing` | The 503 share is above the alert threshold for the window | See [MONITORING.md](MONITORING.md) |
| `ChatLatencyHigh` | p95 of all requests, 503s included, is above its threshold | Same page |

The alert threshold for latency is looser than the answer objective on purpose: a 503 can wait out a model timeout, so counting those would page on every outage twice.

## What we have measured against it

| Evidence | Result | Caveat |
| --- | --- | --- |
| Real-Qwen e2e run, 2026-11-26, normal traffic | About 0.42 s average latency before the fault; about 0.68 to 0.70 s after the small model was disabled (every request then goes to the large model) | One run, a handful of requests, one tester |
| Same run, outage, bad format, bad output, label swap | 0 503s; 8.5 to 12.7 s to disable the model; 50 to 54 s to serve again | Single runs; the fault is injected, not natural |
| Same run, hang | 0 503s, but 30.6 s per hung request and 185.9 s to disable | **This broke the latency objective and the 30 s detection target.** See [POSTMORTEM.md](POSTMORTEM.md) |
| After #135 | Expected about 18 s to disable and 6 s per hung request | **Not measured on real Qwen** |

We have no measurement of availability over days, and no traffic beyond a few testers. The 99% objective is a design target, not a result.

## How to read it on the running system

- **Grafana** at `/grafana`: "Answered, no 503", "Error budget left", "p95 latency vs target", "Share of requests under the latency target".
- **Prometheus** at the internal `:9090`: the queries in `config/metrics.yaml` can be pasted as they are.
- **The health endpoint**: `GET /v1/health` shows each model's window, any intervention and the Jev breaker.

## What would change the objective

- If testers use the real models for more than a few hours a day, re-derive the latency targets from their p95, not from one evaluation run.
- If the GPU instance gets a second host, availability could move toward 99.5%.
- If the gateway runs more than one copy, the monitor and breaker need shared state first; see [KUBERNETES.md](KUBERNETES.md).
