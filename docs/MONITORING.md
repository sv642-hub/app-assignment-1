# Monitoring and recovery runbook

For whoever runs the stack: the demo, the Jetstream2 deployment or a chaos test. It says how the gateway recovers on its own, where to look when something goes wrong, and what to do for each alert.

The [README](../README.md#monitor-and-controller) explains each part in detail; this page puts them together. Everything here is what `main` does today. Thresholds are the defaults, and they are starting points for the mock prototype, not measured values. With the mock backends, latency and failure rates are simulated and say nothing about real models.

## How the gateway recovers on its own

| Problem | What notices it | What the gateway does | How it ends |
| --- | --- | --- | --- |
| Jev is slow or down | The Jev call times out (`APP_JEV_TIMEOUT_SECONDS`, 2 s) or errors | The rule router picks the route: 20 words or more is complex | The next request tries Jev again |
| Jev keeps failing | 5 failures in a row (`APP_JEV_BREAKER_FAILURES`) | The circuit breaker opens and skips Jev for 30 s (`APP_JEV_BREAKER_COOLDOWN_SECONDS`); the rule router routes | After the cool-off, one request tries Jev. Success closes the breaker; a failure opens it again |
| Jev's route answer is invalid | The answer fails its check | The rule router picks the route for that request | Per request |
| A model call fails | The call raises or times out | The other model answers the same request (`fallback_reason: "selected_backend_error"`); the failure goes into the model's window | Per request |
| A model's reply is malformed | The format check | The customer gets a fixed "could not produce a valid reply" message; the failure goes into the window | Per request |
| A model keeps failing | Failure rate of 50% or more (`APP_MONITOR_FAILURE_THRESHOLD`) over its last 20 calls (`APP_MONITOR_WINDOW`), with at least 5 calls (`APP_MONITOR_MIN_SAMPLES`) | The controller disables it for 60 s (`APP_CONTROLLER_COOLDOWN_SECONDS`). Its route's traffic goes to the other model (`selected_backend_disabled`) | Re-enabled with an empty window when the next request, Health check or Prometheus scrape after the cooldown looks at it |
| The failing model is the only one left | Same as above | The controller does not disable it. It logs `kept_last_backend` once and keeps serving | When the rate drops below the threshold |
| A model gives well-formed replies with the wrong intent | The semantic monitor: its intent disagrees with Jev's more than the baseline plus 0.15. Only replies where Jev and the model are both mocks or both real are compared | By default, nothing: status `above_baseline`, a red banner on Health, and an alert. With `APP_SEMANTIC_ACTIONS_ENABLED=true`, the controller disables it like a failing model, but only while the other model is answering | The cooldown, or an operator (see [PossibleWrongIntents](#possiblewrongintents)) |
| The other model fails while one is disabled for wrong intents | The second model's call fails | The model disabled for its intents still answers, as a last resort (`fallback_reason` ending in `semantic_last_resort`): a possibly wrong intent beats a 503. A model disabled for failures or held by an operator is never a last resort | Per request |
| A request runs long | The request deadline (`APP_REQUEST_DEADLINE_SECONDS`, 45 s) | The first model gets the deadline minus 15 s (`APP_ALTERNATE_RESERVE_SECONDS`) when the other model can still answer, so a fallback has time. If the deadline cuts the fallback short, the request gets a 503 with `deadline_exceeded` | Per request |
| No model can answer | Both are unavailable, disabled for failures or held, or both failed this request | 503 `No mock backend is available`. If the other model's cooldown ends during the failing call, it answers instead | When a model is usable again |
| The database is slow or down | A store write takes longer than `APP_STORE_TIMEOUT_SECONDS` (2 s) or fails | The customer still gets the reply, but it can't be looked up by id. Interventions that fail to save are kept and retried in order | When the database answers again |

Two limits to keep in mind:
- **Memory only:** the windows, cooldowns and operator holds live in the gateway process. A restart clears them; the intervention log survives in Postgres. Run one gateway worker.
- **No Alertmanager:** alerts show in Prometheus and nowhere else, so someone has to look.

## Where to look

1. **Health screen:** `http://localhost:8080/#/health`, refreshed every 5 seconds. It shows a banner when a model is out or above the semantic baseline, and a note while Jev is skipped. Below it are the monitor and semantic tables, recent requests and the intervention log.
2. **`GET /v1/health`:** the same data as JSON. The fields that matter most here:
   - `status`: `degraded` when any model is unavailable, disabled or held;
   - `monitor`: per model, the samples, failures, `disabled_by_controller`, `disabled_for` (`failure` or `semantic`), `disabled_by_operator`, `disabled_for_seconds` and `answering`;
   - `interventions`: the last 50, oldest first, each with its reason, such as `format failure rate 100% over last 5 calls`;
   - `jev_breaker`: the state (`closed`, `open` or `half_open`) and consecutive failures;
   - `semantic`: per model, the disagreement rate, coverage, status and `not_compared` (replies skipped because one of Jev and the model is a mock);
   - `usage`: attempted, completed and failed calls per model since startup.
   - `jev_usage`: answered Jev calls, tokens and estimated cost since startup (null cost while `APP_JEV_INPUT_PRICE_PER_MILLION_USD` is unset).
3. **Prometheus alerts.** Prometheus has no published port, so use `exec`:

   ```bash
   docker compose exec prometheus wget -qO- http://127.0.0.1:9090/api/v1/alerts
   ```

4. **Gateway logs.** They hold error types only, never message text or exception messages:

   ```bash
   docker compose logs --since 15m gateway
   ```

   | Log line | Meaning |
   | --- | --- |
   | `Backend qwen3-1.7b failed (TimeoutError)` | One call to that model failed; the type says why |
   | `Jev routing failed; using rule router (timeout)` | One Jev call failed; the rule router routed |
   | `Jev route answer was invalid; using rule router (...)` | Jev answered, but the route answer failed its check |
   | `Could not save request (...)` | A store write failed; the customer still got the reply |
   | `Could not save interventions (...)` | Same for the intervention log; the batch is retried |

5. **The full intervention log** in Postgres:

   ```bash
   docker compose exec postgres psql -U gateway -c 'SELECT at, backend, action, reason FROM interventions ORDER BY seq DESC LIMIT 20'
   ```

## Alerts

[`prometheus/alerts.yml`](../prometheus/alerts.yml) has the rules. Each one links to its section below.

### GatewayDown

**Critical.** Prometheus can't scrape the gateway's `/metrics` for 1 minute. The app can't answer chats.

Check:
- `docker compose ps`: is `gateway` running and healthy?
- `docker compose logs gateway`: a startup error says why it stopped. Common ones:
  - `APP_STORE=postgres` without `APP_DATABASE_URL`;
  - the public default database password with `APP_ENVIRONMENT` set to `staging` or `production`;
  - a settings value out of range.
- Is `postgres` healthy? The gateway waits for it at startup.

Do: fix the cause, then run `docker compose up -d --wait`. Compose restarts a gateway that crashes (`restart: unless-stopped`), so an alert that keeps firing means it can't start.

It clears 1 minute after the scrape works again.

### ModelDisabledByController

**Warning.** The controller disabled a model. Its route's traffic goes to the other model until the cooldown ends (60 s by default). Customers still get answers.

This alert covers the controller only. A model an operator holds shows `disabled_by_operator` on Health, but there is no metric or alert for holds.

Check the newest `disabled` intervention for the model on Health. Its reason says what failed:
- **`format failure rate ...`:** the replies were malformed. With a real model, look at its output. With the mocks, a fault is set: the chaos override, or `POST /v1/faults` with `bad_format`.
- **`call error rate ...`:** calls raised. With a real model, its server is down, unreachable or timing out. With the mocks: the outage override, or `/v1/faults` with `outage`.
- **`failure rate ...`:** a mix of both.
- **`intent differs from Jev's on ...`:** a semantic disable, only with `APP_SEMANTIC_ACTIONS_ENABLED=true`. `disabled_for` is `semantic`, and the model still answers as a last resort. See [PossibleWrongIntents](#possiblewrongintents).

Do:
- **A one-off:** the model is re-enabled after the cooldown with an empty window. Nothing to do.
- **It cycles** between `disabled` and `re-enabled`: the problem is still there. Hold it with an operator disable (see [Operator actions](#operator-actions)) until it is fixed, then enable it.
- **A mock fault from `/v1/faults`:** clear it with `{"model": "...", "fault": "none"}`. For a Compose override, run `docker compose up -d --wait`.

It clears at the first scrape after the model is re-enabled.

### ModelFailureRateRising

**Warning.** A model fails 25% or more of the calls in its window, with at least 5 calls, for 2 minutes. The controller acts at 50%.

This is the early warning. It matters most when the other model is out: the controller never disables the last usable model, so this alert, plus `kept_last_backend` in the intervention log, is the main sign that the only model left is failing.

Check: the model's row on Health (format failures and call errors), and the gateway logs for `Backend ... failed`. Do: the same as [ModelDisabledByController](#modeldisabledbycontroller).

It is meant for partial failures the controller doesn't act on. In a hard failure, the controller disables the model first, and its re-enable clears the window, which restarts this alert's 2-minute timer. So it may never fire there; [ModelDisabledByController](#modeldisabledbycontroller) covers it.

It clears when the window's failure rate drops below 25%.

### PossibleWrongIntents

**Warning.** For 5 minutes, a model's intent has disagreed with Jev's more often than the baseline plus the margin (0.15). Its replies pass the format check, so the failure monitor sees nothing.

It can fire only when `APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE` is set. Without a baseline, the status is `no_baseline` and nothing is flagged.

Check the semantic table on Health:
- **Coverage:** the rate only means something with enough Jev answers behind it. Low coverage gives the status `low_coverage`, not this alert.
- **Mock mode:** Jev and both models share one keyword classifier, and the label-swap override sets a baseline of 0. Any disagreement there is a simulated fault: `compose.labelswap.yml`, or `/v1/faults` with `label_swap`.
- **Real mode:** Jev can be wrong too, and the baseline is a placeholder until it is set from dev data. Look at recent requests before trusting the rate.

Do:
- **Semantic actions off** (the default): the gateway takes no action. If you are confident the model is wrong, hold it with an operator disable, and its route's traffic goes to the other model.
- **Semantic actions on** (`compose.labelswap.yml` turns them on): the controller disables the model once the other one is answering, and [ModelDisabledByController](#modeldisabledbycontroller) fires. If it logs `kept_last_backend` instead, the other model isn't answering, so check it first.
- **Doubtful about the rate** (low coverage, or no baseline from dev data yet): leave semantic actions off.
- **A mock fault:** clear it with `"fault": "none"`.

With semantic actions on, a model giving the wrong intent on every reply is disabled within a few replies. Its re-enable clears the window, so this alert's 5-minute timer restarts and it may not fire; [ModelDisabledByController](#modeldisabledbycontroller) covers that case.

It clears when the model is back within the baseline plus margin.

### ChatRequestsFailing

**Warning.** At least one chat request got a 503 in the last 5 minutes.

The 503's `detail` says which kind:
- **`No mock backend is available`:** no model could answer. Either both are out (unavailable in `config/backends.json`, disabled or held), or the only usable one failed this request.
- **`deadline_exceeded`:** the first model failed or ran long, and the deadline cut the fallback short.

Check: Health's monitor rows for both models, `usage` for failed calls, and the logs.

Do:
- **One model is held and the other is failing:** enable the held one.
- **A mock fault:** clear it.
- **One 503 while both models had a bad moment:** nothing to do.

It clears 5 minutes after the last 503.

### ChatMostlyFailing

**Critical.** More than 1 in 5 chat requests get a 503, for 5 minutes. Customers are mostly not getting answers. It also fires when no request has succeeded since the gateway started.

Do, in order:
1. On Health, find which models are out and why: disabled, held, or unavailable in config.
2. Enable any model an operator holds, if it works.
3. Clear any mock faults (`"fault": "none"` for each model) or return from an override with `docker compose up -d --wait`.
4. With real models, check that their servers are up and reachable from the gateway.
5. As a last resort, `docker compose restart gateway`. This clears the cooldowns, holds, windows and faults set through `/v1/faults`. The intervention log stays in Postgres.

It clears when the share of 503s stays under 20%.

### JevMostlySkipped

**Warning.** For 5 minutes, the rule router has routed more than half of the messages. Either Jev calls fail or time out, its route answers are invalid, or its circuit breaker is open.

Routing still works, but by word count only. The semantic monitor also loses its signal: without Jev's intent answer, replies score `no_signal` and coverage drops. So during a Jev outage [PossibleWrongIntents](#possiblewrongintents) can't fire, and Health shows `low_coverage` instead.

Check:
- `jev_breaker` on Health: an `open` state and its consecutive failures.
- The logs: `Jev routing failed; using rule router (...)`. The type in brackets says why, for example `timeout` or an error code from the client.

Do: in real mode, check the TypeSafe API key in `env/.env`, the network from the gateway, and the provider's status. The gateway needs no action: the breaker tries Jev again every 30 s and closes on the first success. In mock mode this should not happen; if it does, look at the logs.

It clears when most messages are routed by Jev again.

### ChatLatencyHigh

**Warning.** p95 chat latency, measured inside the gateway and including 503s, is above 10 s for 10 minutes.

Check which part is slow:
- **The models:** with the mocks, latency is simulated, 120 ms for the small model and 350 ms for the large one. So in mock mode, high latency points elsewhere. With real models, check their servers.
- **Jev:** each call can take up to its 2 s timeout before the rule router takes over.
- **The database:** during an outage, each store write waits up to 2 s, and requests queue behind failed intervention saves (see [Storage](../README.md#storage)). Look for `Could not save` in the logs.
- **Fallbacks:** a request whose first model fails waits for that model before the other one answers.

It clears when p95 drops back under 10 s.

## Operator actions

Set `APP_OPERATOR_TOKEN` (16 characters or more) in `env/.env` first. Without it, the operator routes answer 403. Never share the token with testers. Caddy proxies `/v1`, so the token is the only guard.

```bash
TOKEN='your-operator-token'
H="Authorization: Bearer $TOKEN"

# Hold a model out until you enable it (refused with 409 for the last usable model)
curl -fsS -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"reason": "small model returning bad JSON"}' \
  http://localhost:8080/v1/operator/models/qwen3-1.7b/disable

# Enable it again: lifts a hold, or ends a controller cooldown early, with an empty window
curl -fsS -X POST -H "$H" http://localhost:8080/v1/operator/models/qwen3-1.7b/enable

# Clear a simulated fault on a mock model
curl -fsS -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"model": "qwen3-1.7b", "fault": "none"}' http://localhost:8080/v1/faults
```

- **The reason is public:** `GET /v1/health` lists it without a token, so never put customer data in it.
- **Holds don't survive a restart:** re-apply a hold after restarting the gateway.
- **Overrides replace the stack:** don't run the Compose fault overrides on the deployment testers use. They take over port 8080 and the default project. Use `/v1/faults`, or another machine.
- **No new ports:** never publish the gateway, Postgres or Prometheus ports to debug. Use `docker compose exec`, as above.
