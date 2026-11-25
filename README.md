# Smart AI Gateway

Who owns what: [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md) · What is built: [docs/architecture.svg](docs/architecture.svg) · Beta testers: [USER_BRIEF.md](USER_BRIEF.md)

A mocked banking-support gateway for DSAN 6700 Deliverable 2, with a React chat app in front of it. Send a customer message and get a reply from a fictional policy card, tagged `standard` or `complex`, with the intent and routing trace one click away. A Health screen shows the models and the self-healing controller.

**By default everything is simulated.** Jev and both Qwen models are mocks, and the app needs no Qwen weights, GPU, torch or Transformers. A real Jev client exists, but it is off unless `APP_JEV_MODE=real` (see Real Jev below); the Qwen backends are always mocks. This prototype demonstrates the user journey in our planned architecture. It does not implement the full monitoring, self-healing, canary-release or linear-probe design.

## What happens to a message

1. The React app sends the message over HTTP to the FastAPI gateway.
2. `MockJevRouter` stands in for Jev's combined call, which answers two questions: the route (`standard` or `complex`) and the intent. It uses the configured complexity and intent of the best keyword match; unmatched messages go to `standard` with intent `unknown`. The gateway uses only the route answer. Jev's intent is an assessment for the monitor and never replaces the backend's intent. The gateway builds a record for each answer and saves it with the request (see Storage).
3. Standard selects mock `qwen3-1.7b`. Complex selects mock `qwen3-4b`. The selected mock backend waits for its simulated delay, detects the intent by matching complete keyword phrases (longer/more matches score higher; config order breaks ties), and fills the reply template with fictional policy facts. Both mock backends share this classifier.
4. Reply checks reject blank text, replies over 4,000 characters, unfilled `{placeholder}` tokens and intent labels outside `config/labels.json`. Pydantic validates the result. The chat shows the reply and its route; **Show details** opens the intent, policy facts and trace.

**Real Jev.** With `APP_JEV_MODE=real`, the gateway sends each message to TypeSafe in place of step 2, using `TypeSafeJevRouter` in `jev.py`: one `/v1/systemone` call with the route question and the intent question over the 78 labels. Every chat message then leaves the server, so use only made-up details, and update `USER_BRIEF.md` before testers use a gateway in this mode. It needs `APP_JEV_API_KEY` and an `https` `APP_JEV_BASE_URL`. The trace shows `router: "jev"` when real Jev chose the route, and `/v1/health` shows `jev_mode: "real"` and `router: "jev"`. Compose and CI run with the default, `mock`. No test calls TypeSafe; the tests use a fake TypeSafe server.

**Real Qwen.** With `APP_PROVIDER_MODE=real`, step 3 calls the two vLLM servers on the GPU host (see [GPU host](#gpu-host-vllm)) through `VLLMBackend` in `vllm.py`, built by `real_backends.py`. Standard messages go to `APP_VLLM_SMALL_BASE_URL` and complex ones to `APP_VLLM_LARGE_BASE_URL`, each with `Authorization: Bearer <APP_VLLM_API_KEY>`; it needs all three. The model writes the reply from the policy card in its system prompt, so the trace shows `reply_source: "generated"`, `mocked: false` and `simulated_latency_ms: 0`, and `/v1/health` shows `mode: "real"`. Each call gets the time the request deadline leaves; the client follows no redirects and makes no retries. A failed call still counts as a call error for the controller, and also gets an error code: `timeout`, `call_failed` (an HTTP error or no connection), `invalid_output` (not a valid `BackendReply`) or `other`. `usage[].failed_by_error` in `/v1/health` and `gateway_backend_errors_total` count them by model. The log line names the code and the exception type, never the message or the provider's text. `/v1/faults` answers 409, because only the mocks have fault rates. The semantic monitor compares only like with like, so real Qwen with mock Jev, or the reverse, is not scored. Compose and CI run with the default, `mock`. No test calls vLLM; the tests use two fake vLLM servers.

If Jev fails or does not answer within `APP_JEV_TIMEOUT_SECONDS`, the rule router chooses the route instead: messages of `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` words or more go to `complex`, shorter ones to `standard`. The request still goes to a backend. The trace shows `router: "rule_router"`, `jev_choice: null`, `confidence: null` and `fallback_reason` `jev_timeout` or `jev_error`. Jev's answers are checked one at a time: an invalid route answer also uses the rule router, with `fallback_reason: "jev_invalid_route"`, while an invalid intent answer still uses Jev's route.

After `APP_JEV_BREAKER_FAILURES` failed Jev calls in a row (default 5), a circuit breaker skips Jev for `APP_JEV_BREAKER_COOLDOWN_SECONDS` (default 30), and the rule router routes every message with `fallback_reason: "jev_circuit_open"`. After the cool-off, one message tries Jev again: success closes the breaker, failure opens it for another cool-off. An invalid answer is not a failed call. `/v1/health` shows the breaker in `jev_breaker`. The breaker does not change `status`, which is about the backends only: while Jev is skipped, customers still get answers from the right models.

If the backend detects no intent, it returns intent `unknown` with a reply asking the customer to describe their banking problem (`reply_source: "no_match"`). A real backend must keep this behaviour. Vague and off-topic messages currently get the same reply. If a backend returns one of the 67 Banking77 intents that has no policy card, it must send an empty reply and no policy facts. The gateway then replies that the demo does not cover that topic (`reply_source: "unsupported_topic"`). The mock backends only match the 10 carded intents, so they never take this path. If the selected backend is marked unavailable, the other is used only when available (`selected_backend_unavailable`). When both are unavailable, every message returns HTTP 503. When Jev fails and the backend falls back, `fallback_reason` lists both reasons.

If the selected backend's call raises an error, or returns a reply that does not match the backend reply schema, the request is tried once on the other usable backend (`selected_backend_error`). Both cases count as failed calls (`call_errors`), not format failures. When no backend can answer, the message returns HTTP 503. The log records the backend and error type, not the message or the provider's error text. Each chat request gets `APP_REQUEST_DEADLINE_SECONDS` in all, counted from before the Jev call. When the other backend is usable, the first attempt gets the time left minus `APP_ALTERNATE_RESERVE_SECONDS`, so the other backend still has time to answer. A first attempt that runs out of time counts as a failed call for its backend. If the other backend is then cut short by the deadline, the message returns HTTP 503 with detail `deadline_exceeded`; that call counts in `deadline_exceeded_calls` in `/v1/health`, not as a failed call. When only one backend is usable, it gets all the time left. If it runs out, that counts as a failed call and the message returns the usual 503, not `deadline_exceeded`.

Confidence is a keyword heuristic, not a calibrated probability. Measured latency includes the simulated wait. A failed format check returns a safe apology (`reply_source: "format_fallback"`) with `format_check: "failed"` and reasons in `format_errors`. The malformed reply is not returned or stored. A passed format check does not establish semantic correctness.

## Monitor and controller

The gateway runs a small in-memory self-healing loop, the first piece of the planned monitor. [docs/MONITORING.md](docs/MONITORING.md) is the runbook: how the pieces below recover together, where to look, and what to do for each alert.

- **Monitor:** for each backend, the gateway records whether each reply passed the format check and whether each call raised an error, over the last `APP_MONITOR_WINDOW` calls (default 20).
- **Controller:** once a backend has at least `APP_MONITOR_MIN_SAMPLES` calls (default 5) and its failure rate (failed format checks plus failed calls) reaches `APP_MONITOR_FAILURE_THRESHOLD` (default 0.5), the controller disables it for `APP_CONTROLLER_COOLDOWN_SECONDS` (default 60). Traffic for that route moves to the other backend with `fallback_reason: "selected_backend_disabled"`. After the cooldown, the backend is re-enabled with an empty window.
- **Action limit:** the controller never disables the last usable backend. It logs `kept_last_backend` instead and keeps serving.
- **Visibility:** the request that triggers an action shows it in `trace.controller_action` and in that reply's details. `GET /v1/health` lists samples, format failures, call errors, failure rate and disabled status for each backend, the last 50 interventions with their reasons, and the controller rules. It also lists `recent_requests`, up to 20 retained requests that got a reply, newest first, with `completed_at`, route, actual model, fallback reason and format check, but no request id. `usage` gives attempted, completed and failed calls for each model since startup, including fallback attempts; token and cost fields are `null` in mock mode. The **Health** screen shows the same data and refreshes every 5 seconds.

The failure monitor watches format failures and failed calls only. It does not know whether an intent or reply is correct. Its windows and which backends are disabled reset when the gateway restarts. The intervention log is kept in the store (see [Storage](#storage)), so with Postgres it survives a restart.

Do not run the overrides below on the deployment testers use. They use the default Compose project and port 8080, so they replace the running stack and reset its history. Use your own machine or a different host.

To see the loop work, start the stack with the chaos override. In `config/backends.chaos.json`, the small model has `fault_rate: 1`, so it corrupts every reply:

```bash
docker compose -f compose.yml -f compose.chaos.yml up -d --build --wait
```

Send five standard messages, such as "My card has not arrived". All five fail the format check, and the fifth disables `qwen3-1.7b`. The sixth is answered by `qwen3-4b`. Open the **Health** screen (`http://localhost:8080/#/health`) to see the model disabled and the intervention. Return to normal with `docker compose up -d --wait`.

To see an outage instead, use the outage override. In `config/backends.outage.json`, the small model has `error_rate: 1`, so every call to it raises an error:

```bash
docker compose -f compose.yml -f compose.outage.yml up -d --build --wait
```

Send five standard messages. Each one is still answered, by `qwen3-4b` with `fallback_reason: "selected_backend_error"`, and the fifth disables `qwen3-1.7b`. Health shows the failed calls and the intervention. Both overrides simulate faults with the mock backends; neither stops a real model.

### Semantic monitor

A model can return a well-formed reply for the wrong intent, which the format check cannot see. The semantic monitor compares each model's intent with Jev's answer to the intent question, from the same request:

- **Samples:** a reply that passed the format check is an `agree` if the model's label matches Jev's, a `disagree` if it differs, and `no_signal` if Jev has no usable intent answer (timeout, error, invalid answer or circuit breaker open). Failed format checks and failed calls give no sample; the failure monitor counts them. `unknown` is a label like any other.
- **Window:** the last `APP_SEMANTIC_WINDOW` samples per model (default 50). A model serving the other route's messages (after a fallback) sees different traffic, so those samples are counted apart, as `rerouted_agreements`, `rerouted_disagreements` and `rerouted_no_signal`.
- **Status** for each model, checked in this order:
  - `low_coverage`: at least `APP_SEMANTIC_MIN_SAMPLES` (default 10) samples, but fewer than `APP_SEMANTIC_MIN_COVERAGE` (default 0.5) had a Jev answer. During a Jev outage, missing data does not look like a healthy model.
  - `collecting`: fewer scored samples than the minimum.
  - `no_baseline`: no healthy baseline is set (`APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE`, unset by default), so rates are reported but never flagged.
  - `above_baseline`: the disagreement rate is above the baseline plus `APP_SEMANTIC_ALERT_MARGIN` (default 0.15).
  - `normal`: otherwise.
- **Visibility:** `GET /v1/health` has `semantic` (per model: counts, `disagreement_rate`, `coverage`, `status`) and `semantic_rules`. `/metrics` has `gateway_semantic_samples_total`, `gateway_semantic_disagreement_rate`, `gateway_semantic_coverage` and `gateway_semantic_above_baseline`. The rate and coverage gauges are absent while a model has no data, so an empty window never reads as 0%.
- **Like with like:** a reply is only compared when Jev and the model are both mocks or both real. Real Jev answers over 78 labels while the mock models know 10 intents, so a mix would disagree by design. Those replies are counted in `not_compared` in `/v1/health` and `gateway_semantic_not_compared_total`, and stay out of the windows.

- **Semantic actions:** off by default (`APP_SEMANTIC_ACTIONS_ENABLED=false`), so the monitor only reports. When they are on and a model's status is `above_baseline`, the controller disables it for `APP_CONTROLLER_COOLDOWN_SECONDS`. It does so only while the other model is serving and has answered recently without failing: its failure window has calls, and its failure rate is below `APP_MONITOR_FAILURE_THRESHOLD`. A model that fails every call stays usable until its own window fills, and an empty window after a re-enable says nothing, so neither counts. Otherwise the controller logs `kept_last_backend` once. The other model can still start failing after the disable. So a model disabled only for its intents stays a last resort: when no other model answers, the gateway calls it anyway, with `fallback_reason` ending in `semantic_last_resort`, because a reply with a possibly wrong intent beats a 503. A model disabled for failures is never a last resort. A last resort keeps being called even if some of its own calls fail, like the last enabled model: a model that answers some calls beats a certain 503. If a model's cooldown ends while another model's call is running, the gateway tries it in the same request. This holds with semantic actions off too: when the only usable model fails, a model whose failure cooldown ended during that call answers the request instead of a 503. `/v1/health` shows `disabled_for` (`failure` or `semantic`) and `answering` for each model. The intervention reason gives the disagreement rate and the baseline. Only replies on the model's own route can trigger an action; a rerouted reply never does. Low coverage and a missing baseline never cause an action. With actions on, any re-enable also starts the model's semantic window again; with actions off, the semantic report keeps its window across a cooldown.

The **Health** screen shows the monitor as a table, one row per model. While a model is above the baseline, a red banner says so, and says whether the controller acted. Keep semantic actions off until the baseline comes from dev data: a wrong baseline would take a healthy model out of service. The defaults are placeholders: the baseline, margin and coverage minimum should be set from dev data before the final test. In mock mode, Jev and both models share one keyword classifier, so a healthy mock always agrees and its baseline is 0. A disagreement rate measured on mocks says nothing about real model quality. The windows reset when the gateway restarts.

To see it work, use the label-swap override. In `config/backends.labelswap.json`, the small model has `label_swap_rate: 1`: it answers every matched message with the next intent's label and policy card. The override also sets the mock baseline of 0 and turns semantic actions on:

```bash
docker compose -f compose.yml -f compose.labelswap.yml up -d --build --wait
```

Send one complex message first, such as "My card was stolen", so the controller has seen `qwen3-4b` answer. Then send ten standard messages, such as "My card has not arrived". Each one gets a well-formed reply about a lost or stolen card, and every format check passes. The tenth puts `qwen3-1.7b` at a disagreement rate of 1.0, status `above_baseline`, and the controller disables it. The eleventh is answered correctly by `qwen3-4b`, with `fallback_reason: "selected_backend_disabled"`. The **Health** screen shows the banner, the disabled model and the intervention. To watch the monitor only report, add `APP_SEMANTIC_ACTIONS_ENABLED: "false"` to the override. Return to normal with `docker compose up -d --wait`.

### Operator routes

Operators can act on the models without restarting the stack. Every operator route needs the header `Authorization: Bearer <APP_OPERATOR_TOKEN>`. While `APP_OPERATOR_TOKEN` is unset, which is the default, the routes answer 403. A missing or wrong token gets 401. Caddy proxies `/v1`, so the token is the only guard: set a long random one (at least 16 characters; leading and trailing spaces are dropped) only in `env/.env`, and never share it with testers.

- `POST /v1/operator/models/{name}/disable` holds a model disabled until an operator enables it. There is no cooldown. Like the controller, it refuses to disable the last usable model (409). Traffic moves with `fallback_reason: "selected_backend_disabled"`.
- `POST /v1/operator/models/{name}/enable` lifts an operator hold, and also ends a controller cooldown early. The model starts with an empty window.
- Holds live in gateway memory. After a restart every held model serves again, even though the last stored intervention for it still says `operator_disabled`. Disable it again after a restart.
- Both take an optional JSON body `{"reason": "..."}` (up to 200 characters), saved in the intervention log as `operator_disabled` or `operator_enabled`. The reason is public: `GET /v1/health` lists it without a token. Do not put customer data or anything private in it. Both return the monitor rows; `disabled_by_operator` shows a hold.
- `POST /v1/faults` sets a simulated fault on a mock model while it runs, in place of restarting with a Compose override. The body is `{"model": "qwen3-1.7b", "fault": "outage", "rate": 1}`. `fault` is `outage` (sets `error_rate`), `bad_format` (`fault_rate`), `label_swap` (`label_swap_rate`) or `none` (clears all three). `rate` is 0–1 and defaults to 1. It returns the model's config. Faults reset when the gateway restarts.
- `POST /v1/faults/jev` does the same for the mock Jev router. The body is `{"fault": "outage", "rate": 1}`. `fault` is `outage` (sets `error_rate`: the call fails, the gateway uses the rule router with `fallback_reason: "jev_error"`, and 5 failures in a row open the circuit breaker), `slow` (sets `slow_rate` and `slow_seconds`: each call it hits is held for `seconds` more, above 0 and up to 120, which `slow` requires; past `APP_JEV_TIMEOUT_SECONDS`, 2 s by default, the gateway uses the rule router with `jev_timeout`) or `none` (clears both). It returns the Jev fault settings. With `APP_JEV_MODE=real` it answers 409: real Jev can't be faulted.

```bash
TOKEN='your-operator-token'
curl -fsS -X POST http://localhost:8080/v1/faults \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model": "qwen3-1.7b", "fault": "outage"}'
curl -fsS -X POST http://localhost:8080/v1/operator/models/qwen3-1.7b/enable \
  -H "Authorization: Bearer $TOKEN"
```

## Run with Docker Compose

Requires Docker with Compose **2.24.0 or newer** for the optional env-file syntax. The clone commands use `main` and apply after this prototype PR is merged.

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git
cd app-assignment-1
docker compose up -d --build --wait
```

Open [the chat](http://localhost:8080) and click an example or type your own message, then press Enter. The **Health** screen is in the sidebar. [API documentation](http://localhost:8080/docs) and the [health endpoint](http://localhost:8080/v1/health) use the same port.

The stack runs `proxy`, `gateway` and `postgres` for the app. `postgres` keeps request history and the controller's interventions (see [Storage](#storage)). `proxy` is Caddy (pinned to 2.11.7 and the tested multi-platform image digest) with the built React app baked in: [`frontend/Dockerfile`](frontend/Dockerfile) builds it with Node, then copies only the static files into the Caddy image. Caddy serves the app at `/` and proxies `/v1/*`, `/docs`, `/redoc` and `/openapi.json` to `gateway`. Only Caddy publishes a host port (`8080`); gateway port `8000` and Postgres port `5432` stay inside the Compose network. All services restart unless stopped. The gateway starts once Postgres is healthy, and the proxy once the gateway is healthy. A fourth service, `prometheus`, collects gateway metrics; see [Metrics and Prometheus](#metrics-and-prometheus).

```bash
docker compose ps
python3 scripts/smoke_compose.py http://localhost:8080
docker compose logs --tail=100
# Stop the prototype when finished:
docker compose down
```

In Compose, request history and interventions are kept in Postgres and survive a gateway restart. Run one gateway worker: the monitor's windows are still per process. No model volume is needed for this prototype. JSON configuration persists in the repository and is mounted read-only.

## Storage

The gateway keeps three kinds of records behind a small `Store` interface ([`store.py`](src/app_assignment_1/store.py)):

- **Completed requests:** each `ChatResponse` (reply, intent, policy facts, trace), which `GET /v1/requests/{id}` and the Health screen's recent requests read. Postgres keeps every request, so they can be analysed later; set `APP_POSTGRES_REQUEST_LIMIT` to keep only the newest ones. The memory store keeps the most recent `APP_REQUEST_STORE_LIMIT` (default 1,000). Requests that end in a 503 are not stored. Each request's trace has `settings_version`, a short ID of the settings and config files that answered it, so statistics can be grouped by setup. A fault set through `/v1/faults` changes it, and clearing the fault changes it back. Secrets, the user and password in a URL, and how a config path is spelled are left out of it.
- **Controller interventions:** every `disabled`, `re-enabled` and `kept_last_backend` entry. `GET /v1/health` shows the last 50.
- **Jev assessments:** two per request, one for the route question and one for the intent question, for the semantic monitor. They are saved in the same write as their request, so a request that ends in a 503 or is cancelled saves none. Postgres keeps them all, also after their request is deleted; the memory store keeps the last 2 × `APP_REQUEST_STORE_LIMIT`. The route record has `option_order`, the order Jev was shown the route options in, for the position-bias study (#86). The intent options follow `config/labels.json`, whose order is part of `option_set_version`.
- **Export for analysis:** `APP_DATABASE_URL=... uv run python scripts/export_analysis.py --out exports/<name>` writes `requests.jsonl` and `jev_assessments.jsonl`, which join on the request ID. Reply text and policy facts are left out. `exports/` is ignored by git.

The customer's message is not stored: `ChatResponse` has no message field. The monitor's windows and which backends are disabled stay in process memory and reset on restart. The backend response records for the monitor are not stored yet; they will get their own store methods and migration once their schema is agreed.

There are two stores, chosen with `APP_STORE`:

- `memory` (the default, and what the tests use): process memory, lost when the gateway restarts.
- `postgres`: a Postgres database at `APP_DATABASE_URL`. Compose uses this. If `APP_STORE=postgres` and the URL is missing, the gateway refuses to start with a clear error.

If a write to the store fails or takes longer than `APP_STORE_TIMEOUT_SECONDS` (default 2), the customer still gets the reply and the gateway logs a warning with the error type only; that reply then cannot be looked up by id. A request makes at most two writes (interventions, then the request), and the limit applies to each write, not to the whole wait. Intervention writes run one at a time. During a database outage, once an intervention batch fails it goes back into the queue, so every later request finds interventions to save and waits for the requests ahead of it: with k requests waiting at once, the last reply is delayed by about k times the limit. In a test with a 0.2 s limit and 6 requests at once, the replies came back after 0.2, 0.4, 0.6, 0.8, 1.0 and 1.2 s. A request that is cancelled during this wait still finishes its writes first. Interventions that fail to save are retried on the next request, health check or shutdown, in their original order. If Postgres is unreachable, `GET /v1/health` and `GET /v1/requests/{id}` return an error.

The database URL holds the password, so the gateway keeps it out of its logs and error messages: psycopg's own connection warnings are turned off, a malformed URL fails startup without being quoted, and settings errors do not print input values.

**Driver and schema.** The Postgres store uses [psycopg 3](https://www.psycopg.org/psycopg3/) with its connection pool and plain SQL. The schema lives in numbered SQL files in [`src/app_assignment_1/migrations/`](src/app_assignment_1/migrations/). At startup the gateway applies any file not yet listed in the `schema_migrations` table, each in one transaction, under a lock so two processes cannot apply the same file. To change the schema, add the next numbered file (for example `002_monitor_records.sql`); never edit a file that has already run.

**Password.** [`env/compose.defaults.env`](env/compose.defaults.env) holds a throwaway password so `docker compose up` and the CI smoke test work with no private files. **It is public and not for deployment.** The gateway logs a warning when it uses it, and refuses to start with it when `APP_ENVIRONMENT` is `staging` or `production`. The database port is not published, so only containers on the Compose network can reach it. Before deploying, set your own password in two private files that Git ignores. Do this before the first start: Postgres reads the password only when its volume is first created.

```bash
PW=$(openssl rand -hex 24)
if [ ! -e env/postgres.env ]; then
  printf 'POSTGRES_PASSWORD=%s\n' "$PW" > env/postgres.env
  # Edit the line instead if env/.env already sets APP_DATABASE_URL.
  printf 'APP_DATABASE_URL=postgresql://gateway:%s@postgres:5432/gateway\n' "$PW" >> env/.env
  chmod 600 env/postgres.env env/.env
fi
```

To change the password of an existing volume, run `ALTER USER gateway PASSWORD '...'` in `docker compose exec postgres psql -U gateway`, then update both files and run `docker compose up -d --force-recreate`. `docker compose down` keeps the `postgres-data` volume. `docker compose down -v` deletes it and all stored history.

To look at the data:

```bash
docker compose exec postgres psql -U gateway -c 'SELECT completed_at, route, model, intent, format_check FROM requests ORDER BY seq DESC LIMIT 5'
```

## Metrics and Prometheus

The gateway serves Prometheus metrics at `GET /metrics` on port 8000. This path is internal: it is not under `/v1`, it is not in the API docs, and Caddy does not proxy it, so `http://localhost:8080/metrics` returns the React app. The smoke test checks this.

The `prometheus` service (Prometheus 3.15.1, config in [`prometheus/prometheus.yml`](prometheus/prometheus.yml)) scrapes `gateway:8000/metrics` every 15 seconds over the Compose network. It publishes no port. Its data is kept in the `prometheus-data` volume until you run `docker compose down -v`.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `gateway_chat_requests_total` | `route`, `model`, `reply_source`, `format_check` | Chat requests that got a reply |
| `gateway_chat_unavailable_total` | none | Chat requests that got a 503 |
| `gateway_chat_latency_seconds` | none | Histogram of time spent in the gateway per chat request, including 503s |
| `gateway_backend_calls_attempted_total`, `_completed_total`, `_failed_total` | `model` | Backend calls, the same numbers as `usage` in `/v1/health` |
| `gateway_jev_fallbacks_total` | `reason` (`jev_timeout`, `jev_error`, `jev_invalid_route`) | Requests routed by the rule router because Jev failed or its route answer was invalid |
| `gateway_model_disabled` | `model` | 1 while the controller has the model disabled |
| `gateway_monitor_failure_rate`, `gateway_monitor_window_samples` | `model` | Failure rate and number of calls in the monitor window (rate is 0 when the window is empty) |
| `gateway_controller_interventions_total` | `action`, `model` | Controller actions: `disabled`, `re-enabled`, `kept_last_backend` |
| `gateway_jev_answered_calls_total` | `mocked` | Jev calls that returned an answer; each call counts once, not once per question |
| `gateway_jev_tokens_total` | `direction`, `mocked` | Input and output tokens of answered Jev calls |
| `gateway_jev_estimated_cost_usd_total` | `mocked` | Input tokens times `APP_JEV_INPUT_PRICE_PER_MILLION_USD`; absent while the price is unset, so an unknown price never reads as $0 |

Labels only take values from fixed lists. Message text, replies and request IDs are never labels; a test checks this. All values are in memory and reset when the gateway restarts. A re-enable is counted when the gateway next checks the model (a request, `/v1/health` or a scrape), not at the exact end of the cooldown. With the mock backends, latency includes the simulated delay and says nothing about real model speed. Jev tokens and estimated cost are counted since the gateway started; mock Jev reports 0 tokens. `/v1/health` has the same totals in `jev_usage`, and the Health screen shows them on the Jev card. There are no Qwen token or cost metrics yet, and Prometheus does not scrape vLLM or a separate monitor because neither runs with the gateway yet.

### Alerts

[`prometheus/alerts.yml`](prometheus/alerts.yml) has 8 alert rules, and [docs/MONITORING.md](docs/MONITORING.md) says what to do for each one. Prometheus evaluates the rules every 15 seconds. There is no Alertmanager yet, so nothing is sent anywhere: firing alerts show in Prometheus's `/alerts` page and `/api/v1/alerts`. The thresholds are starting points for the mock prototype, not measured values.

| Alert | Severity | Fires when |
| --- | --- | --- |
| `GatewayDown` | critical | Prometheus can't scrape the gateway for 1 minute |
| `ModelDisabledByController` | warning | The controller has a model disabled |
| `ModelFailureRateRising` | warning | A model fails 25% or more of the calls in its window, with at least 5 calls, for 2 minutes. The controller acts at 50% by default |
| `PossibleWrongIntents` | warning | A model is above the semantic baseline for 5 minutes |
| `ChatRequestsFailing` | warning | Any chat request got a 503 in the last 5 minutes |
| `ChatMostlyFailing` | critical | More than 20% of chat requests get a 503 for 5 minutes |
| `JevMostlySkipped` | warning | The rule router routes more than half of messages for 5 minutes, because Jev fails or its breaker is open |
| `ChatLatencyHigh` | warning | p95 chat latency is above 10 s for 10 minutes |

**Hard failures and slow drift.** `ModelFailureRateRising` and `PossibleWrongIntents` are for problems the controller does not act on: a model failing some calls but below the 50% threshold, or wrong intents while semantic actions are off or the other model can't take over. For a hard failure, the controller disables the model within a few requests, and `ModelDisabledByController` fires. The re-enable then clears the window, which restarts the `for:` timer of the two slower alerts, so they may never fire during a hard failure. `PossibleWrongIntents` also needs `APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE`: without a baseline, its gauge stays 0 at any disagreement rate.

`ChatMostlyFailing` and `JevMostlySkipped` divide by the number of replies, and `gateway_chat_requests_total` has no series until the first reply. Each rule adds `or vector(0)` to that term, so a full outage right after a restart still fires.

`prometheus/alerts_test.yml` tests each alert with promtool, including cases that must not fire. CI runs it, and checks that Compose's Prometheus loaded the rules. `tests/test_alert_rules.py` checks that every metric the rules use is one the gateway serves. To run the promtool tests locally:

```bash
docker run --rm -v "$PWD/prometheus:/etc/prometheus:ro" -w /etc/prometheus --entrypoint promtool prom/prometheus:v3.15.1 test rules alerts_test.yml
```

To see firing alerts in the running stack:

```bash
docker compose exec prometheus wget -qO- http://127.0.0.1:9090/api/v1/alerts
```

To look at metrics without publishing a port:

```bash
# Raw gateway metrics
docker compose exec gateway python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"

# Is Prometheus scraping the gateway? Look for "health":"up".
docker compose exec prometheus wget -qO- http://127.0.0.1:9090/api/v1/targets

# Query Prometheus, e.g. chat requests per model
docker compose exec prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=sum%20by%20(model)%20(gateway_chat_requests_total)'
```

Prometheus has no published port, including on your own machine. Use the `docker compose exec` commands above to read metrics and run queries.

## Configuration and env files

Defaults work without an env file. To create optional overrides **without replacing an existing file**:

```bash
if [ ! -e env/.env ]; then
  cp env/.env.example env/.env
fi
```

Review an existing file before using it. Old Assignment 2 keys such as `APP_LLM_MODEL_NAME`, `APP_LLM_MAX_NEW_TOKENS`, `APP_API_KEY`, `APP_DEBUG`, `APP_HOST`, `APP_PORT` and `APP_LOG_LEVEL` no longer belong to this prototype. Remove obsolete keys manually; do not overwrite your existing file. Unknown dotenv keys fail validation.

Pydantic Settings reads `.env` then `env/.env` in native local runs. Process environment variables take precedence. Compose injects `env/.env` at runtime; no real env file is copied into the image. The frontend needs no settings: it calls the API on its own origin.

| Setting | Default | Purpose |
| --- | --- | --- |
| `APP_ENVIRONMENT` | `local` | `local`, `staging` or `production` |
| `APP_INTENTS_PATH` | `config/intents.json` | Intent keywords and mock complexity |
| `APP_LABELS_PATH` | `config/labels.json` | The 77 Banking77 intents plus `unknown` that Jev and Qwen classify into; every intent with a policy card must be one of them |
| `APP_POLICY_CARDS_PATH` | `config/policy_cards.json` | Fictional facts and `{facts}` reply templates |
| `APP_BACKENDS_PATH` | `config/backends.json` | Backend names, routes, delay and availability |
| `APP_REQUEST_STORE_LIMIT` | `1000` | Number of successful requests the memory store keeps |
| `APP_POSTGRES_REQUEST_LIMIT` | unset | Unset keeps every request in Postgres, for analysis; a number keeps only the newest ones |
| `APP_JEV_TIMEOUT_SECONDS` | `2` | Wait for the Jev routing call before using the rule router |
| `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` | `20` | Rule router sends messages this long or longer to `complex` |
| `APP_MONITOR_WINDOW` | `20` | Recent calls per backend that the monitor keeps |
| `APP_MONITOR_MIN_SAMPLES` | `5` | Calls needed before the controller may act; at most the window |
| `APP_MONITOR_FAILURE_THRESHOLD` | `0.5` | Failure rate (failed format checks plus failed calls) that disables a backend |
| `APP_CONTROLLER_COOLDOWN_SECONDS` | `60` | How long a disabled backend stays out |
| `APP_STORE` | `memory` | `memory` (lost on restart) or `postgres`; Compose sets `postgres` |
| `APP_DATABASE_URL` | none | Postgres URL, required when `APP_STORE=postgres`; a secret, hidden in logs and settings output |
| `APP_STORE_TIMEOUT_SECONDS` | `2` | Longest wait for one store write before the reply goes out without it |
| `APP_JEV_MODE` | `mock` | `mock` or `real`. Real sends each chat message to TypeSafe for the route and intent answers; it needs `APP_JEV_API_KEY` and an `https` `APP_JEV_BASE_URL` |
| `APP_PROVIDER_MODE` | `mock` | `mock` or `real` for the Qwen backends. Real calls the two vLLM servers and needs `APP_VLLM_SMALL_BASE_URL`, `APP_VLLM_LARGE_BASE_URL` and `APP_VLLM_API_KEY` |
| `APP_JEV_BASE_URL` | `https://api.typesafe.ai` | TypeSafe API, used when `APP_JEV_MODE=real` |
| `APP_JEV_API_KEY` | unset | Server-side TypeSafe key; set only in the real env file |
| `APP_JEV_MODEL` | `jev-1.14.0` | Pinned Jev version, not an alias such as `jev-latest` |
| `APP_JEV_INPUT_PRICE_PER_MILLION_USD` | unset | Jev input token price for cost estimates; Jev does not charge for output tokens. Cost stays null while unset |
| `APP_JEV_BREAKER_FAILURES` | `5` | Consecutive Jev failures before the circuit breaker skips Jev |
| `APP_JEV_BREAKER_COOLDOWN_SECONDS` | `30` | How long the breaker skips Jev |
| `APP_OPERATOR_TOKEN` | unset | Bearer token for the operator routes, at least 16 characters; unset turns them off (403). Set it only in `env/.env` |
| `APP_VLLM_SMALL_BASE_URL` | unset | Qwen3-1.7B server on the GPU host's private IP, for example `http://10.0.12.34:8001`; read when `APP_PROVIDER_MODE=real` |
| `APP_VLLM_LARGE_BASE_URL` | unset | Qwen3-4B server on the GPU host's private IP, for example `http://10.0.12.34:8002`; read when `APP_PROVIDER_MODE=real` |
| `APP_VLLM_API_KEY` | unset | The key both vLLM servers require (`VLLM_API_KEY` in `env/gpu.env`). Set it only in `env/.env`; sent as a bearer token when `APP_PROVIDER_MODE=real` |
| `APP_REQUEST_DEADLINE_SECONDS` | `45` | Total time for one chat request, including the Jev call. A first attempt that runs out of time counts as a failed call; an alternate cut short gives a 503 with `deadline_exceeded` |
| `APP_ALTERNATE_RESERVE_SECONDS` | `15` | Part of the deadline held back from the first attempt for the other backend, only when the other backend is usable; with the Jev timeout, must stay below the deadline |

Paths are relative to the working directory: run native commands from the repo root. In Docker the working directory is `/app`; use paths under the mounted `config/` directory. The gateway validates JSON and matching intent/policy-card coverage at startup. Both mock backends must be configured. Set `available` to `false` in `config/backends.json` to try fallback.

Settings are cached and JSON files are loaded once at startup. After editing JSON files run `docker compose restart gateway`. After changing env values run `docker compose up -d --force-recreate`. Tests may call `get_settings.cache_clear()` to reread settings. Changing a model label does not run a real model.

Use public or fictional demo messages. The mock does not send messages to Jev or another model provider. It has no user authentication. It stores responses (not messages) in process memory or, in Compose, in Postgres.

## Run natively with uv

Use Python 3.13 as in CI. Install [uv](https://docs.astral.sh/uv/) first.

```bash
uv sync --extra dev --frozen
uv run uvicorn app_assignment_1.api:app --host 127.0.0.1 --port 8000
```

Native runs use the memory store unless you set `APP_STORE=postgres` and an `APP_DATABASE_URL` your machine can reach. The Compose database is not reachable from the host, by design.

In another terminal, start the React app with Node 22:

```bash
cd frontend
npm ci
npm run dev
```

Open the address Vite prints (usually `http://localhost:5173`). The dev server reloads on every save and proxies `/v1`, `/docs`, `/redoc` and `/openapi.json` to the gateway on port 8000, so the app uses the same paths as behind Caddy. Set `GATEWAY_URL` to point it at another gateway.

## API examples

```bash
curl -fsS http://localhost:8080/v1/health

curl -fsS http://localhost:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"My card has not arrived"}'

curl -fsS http://localhost:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"I lost my card"}'

curl -fsS http://localhost:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Tell me about the weather"}'
```

A standard response has this shape (ID and measured latency vary):

```json
{
  "id": "d966fd25-4630-4289-8ce4-7a8c4a2afc6a",
  "intent": "card_arrival",
  "reply": "For this fictional DemoBank scenario: DemoBank standard cards arrive within 5 business days. Track delivery in the Cards section of the fictional app.",
  "policy_facts": [
    "DemoBank standard cards arrive within 5 business days.",
    "Track delivery in the Cards section of the fictional app."
  ],
  "trace": {
    "router": "mock_jev",
    "jev_choice": "standard",
    "route": "standard",
    "model": "qwen3-1.7b",
    "reply_source": "policy_template",
    "confidence": 0.87,
    "latency_ms": 121.4,
    "simulated_latency_ms": 120,
    "format_check": "passed",
    "format_errors": [],
    "fallback_reason": null,
    "controller_action": null,
    "mocked": true
  }
}
```

Retrieve a response using its returned ID:

```bash
REQUEST_ID='paste-the-returned-id-here'
curl -fsS "http://localhost:8080/v1/requests/$REQUEST_ID"
```

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/chat` | 200 validated reply; 422 invalid input; 503 no backend available or able to answer, or `deadline_exceeded` |
| `GET /v1/requests/{id}` | 200 stored response; 404 missing, evicted, or lost in a restart with the memory store; 422 invalid UUID |
| `GET /v1/health` | Typed Pydantic response documented in OpenAPI; 200 with backend config, monitor statistics, recent interventions, recent requests (`recent_requests`) and per-model call totals (`usage`); `degraded` when any backend is unavailable or disabled by the controller or an operator |
| `POST /v1/operator/models/{name}/disable`, `/enable` | Operator token required. 200 monitor rows; 401 missing or wrong token; 409 last usable model (disable); 422 unknown model; 403 `APP_OPERATOR_TOKEN` unset |
| `POST /v1/faults` | Operator token required. 200 the model's config; 401, 403 and 422 as above |
| `POST /v1/faults/jev` | Operator token required. 200 the Jev fault settings; 409 when Jev is real; 401, 403 and 422 as above |
| `GET /docs`, `/redoc`, `/openapi.json` | Interactive documentation and API schema |

Messages must contain 1–2,000 characters and cannot be whitespace-only. Extra request fields are rejected.

## Deploy on Jetstream2

Unshelve the instance and confirm its current public IP first. Replace `YOUR_INSTANCE_IP` below with its current public IP. SSH username is `exouser`. The following assumes Docker with Compose 2.24+ is already installed and your account can run Docker.

From your laptop:

```bash
ssh exouser@YOUR_INSTANCE_IP
```

Before building, inspect disk usage and stop the old Assignment 2 stack from its checkout. This stops that earlier service; do this only when it is no longer needed for grading.

```bash
docker compose ls
docker system df
# Change to the actual old checkout shown by docker compose ls.
cd /path/to/old/assignment-checkout
docker compose images
docker compose down
```

Stopping containers does not reclaim their images or model cache. If space is still short, inspect resources before optionally deleting the old ones:

```bash
docker image ls
docker volume ls
# Substitute verified old resource names/IDs; do not use these placeholders literally.
docker image inspect OLD_IMAGE_ID
docker volume inspect OLD_MODEL_VOLUME
# Optional: only after confirming these belong to the retired assignment stack.
docker image rm OLD_IMAGE_ID
# Permanently deletes that model cache. Skip if you need to preserve it.
docker volume rm OLD_MODEL_VOLUME
docker system df
df -h /
```

Do not use a global prune command or delete resources belonging to other projects. Return to your home directory and use a separate checkout for this prototype:

```bash
cd ~
```

On the instance:

```bash
df -h /
git clone --branch main --single-branch \
  https://github.com/sv642-hub/app-assignment-1.git smart-ai-gateway-prototype
cd smart-ai-gateway-prototype
docker compose version
```

Before starting Compose, set your own database password as described in [Storage](#storage) and set `APP_ENVIRONMENT=production` in `env/.env`. The production template sets this for you. The gateway then refuses the public database password and demo operator token. Keep `compose.demo.yml` on the local demo stack.

```bash
docker compose -p smart-gateway-prototype up -d --build --wait
docker compose -p smart-gateway-prototype ps
curl -fsS http://127.0.0.1:8080/v1/health
python3 scripts/smoke_compose.py http://127.0.0.1:8080
```

These deployment commands target `main` after this PR is merged. For updates:

```bash
cd ~/smart-ai-gateway-prototype
git pull --ff-only
docker compose -p smart-gateway-prototype up -d --build --wait
```

Allow inbound TCP **8080** in the instance's security group and host firewall. Keep the existing SSH access rule. Do not publish the gateway or database ports. If 8080 is already occupied, identify that service before stopping anything.

Back on your laptop, verify external access:

```bash
curl -fsS http://YOUR_INSTANCE_IP:8080/v1/health
curl -fsS http://YOUR_INSTANCE_IP:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"My card has not arrived"}'
```

Open `http://YOUR_INSTANCE_IP:8080` and send a chat message. Leave Compose running for grading. Local verification does not confirm instance deployment or external reachability.

## GPU host (vLLM)

The two Qwen models run on a separate Jetstream2 GPU instance, using [`compose.gpu.yml`](compose.gpu.yml). The gateway does not call them yet: it still uses the mock backends until the Qwen backends are wired in. This section sets up the servers, so the client and the probe have something real to test against.

| Server | Model and pinned revision | Served name | Address |
| --- | --- | --- | --- |
| `vllm-small` | `Qwen/Qwen3-1.7B` @ `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | `qwen3-1.7b` | `http://GPU_PRIVATE_IP:8001` |
| `vllm-large` | `Qwen/Qwen3-4B` @ `1cfa9a7208912126459214e8b04321603b3df60c` | `qwen3-4b` | `http://GPU_PRIVATE_IP:8002` |

Both run vLLM `v0.33.0` (image pinned by digest) with `--max-model-len 4096` on one GPU. The served names match `config/backends.json`.

**Network.** vLLM is reachable only from the CPU instance, over the Jetstream2 private network. Each server binds to the GPU host's private IP (`VLLM_BIND_IP`), and Compose will not start without it. Do not bind to `0.0.0.0` or the public IP. Do not open 8001–8002 to the internet.

**API key.** Every `/v1` request must also send `Authorization: Bearer <key>`, with the key from `VLLM_API_KEY` in `env/gpu.env`. Compose will not start without it. The network rules stay the main protection; the key covers a security group that gets loosened by mistake. `/health` and `/metrics` need no key. The gateway sends the same value, from `APP_VLLM_API_KEY`, when `APP_PROVIDER_MODE=real`. The gateway calls vLLM over plain HTTP on the private network, so the key is not encrypted in transit.

**Allocate the instance** (once):

1. Create a GPU instance in the same project and private network as the CPU instance. Pick a GPU with at least 20 GB of memory: the two models' bf16 weights are about 12 GB together, and the rest goes to KV cache. The settings in `compose.gpu.yml` are sized for the GRID A100X-20C vGPU, where about 2.16 GiB of the 20 GiB is reserved and not shown by `nvidia-smi`. The vLLM image alone takes about 37 GB of the root disk, so attach a separate Jetstream2 volume (50 GB is plenty) for the model weights, for example at `/media/volume/gpu-models`.
2. Add a security group that keeps SSH and allows TCP 8001–8002 **only from the CPU instance's private IP**.
3. Install Docker with Compose 2.24+ and the NVIDIA Container Toolkit. Check with `docker run --rm --gpus all ubuntu nvidia-smi`.

**Start the servers** on the GPU instance:

```bash
git clone --branch main --single-branch \
  https://github.com/sv642-hub/app-assignment-1.git smart-ai-gateway-prototype
cd smart-ai-gateway-prototype
cp env/gpu.env.example env/gpu.env   # set VLLM_BIND_IP (ip -4 addr) and MODEL_CACHE_DIR (the attached volume)
sed -i "s/^VLLM_API_KEY=$/VLLM_API_KEY=$(openssl rand -hex 32)/" env/gpu.env
docker compose -f compose.gpu.yml --env-file env/gpu.env -p smart-gateway-gpu up -d --wait
docker compose -f compose.gpu.yml --env-file env/gpu.env -p smart-gateway-gpu ps
nvidia-smi
```

The first start downloads both models (about 12 GB) into `MODEL_CACHE_DIR`, which can take several minutes. `vllm-large` starts only after `vllm-small` is healthy. If `vllm-small` runs out of GPU memory, add `--enforce-eager` to it, or adjust `--gpu-memory-utilization` (0.33 and 0.54; together they must fit in the memory CUDA reports free, not the full 20 GiB), in `compose.gpu.yml` through a pull request.

After the first download, set `HF_HUB_OFFLINE=1` in `env/gpu.env` so later starts never contact Hugging Face.

The memory margin is tiny: `vllm-large` starts with about 0.02 GiB to spare. If one container restarts while the other is running, its memory check can fail. Restart both together with `down` and then `up -d --wait`, not one at a time. The containers also do not come back by themselves after the instance is shelved and unshelved; run the same `up -d --wait` command.

**Share the key with the CPU instance.** The key lives in two places only: `env/gpu.env` on the GPU host, and `~/.vllm_api_key` on the CPU instance (readable only by `exouser`). Copy it from your laptop; it goes straight from one host to the other and never shows on screen:

```bash
ssh exouser@GPU_PUBLIC_IP 'sed -n "s/^VLLM_API_KEY=//p" ~/GPU_CHECKOUT/env/gpu.env' \
  | ssh exouser@CPU_PUBLIC_IP 'umask 077; cat > ~/.vllm_api_key'
ssh exouser@CPU_PUBLIC_IP 'wc -c < ~/.vllm_api_key'   # 65: 64 characters and a newline
```

`GPU_CHECKOUT` is the folder you cloned into on the GPU host. Do not send the key through GitHub, team chat or email. To change it, set a new `VLLM_API_KEY` in `env/gpu.env`, restart both servers (`down`, then `up -d --wait`) and copy it again.

**Check from the CPU instance**, in a checkout with `uv sync --extra dev --frozen`:

```bash
export VLLM_API_KEY=$(cat ~/.vllm_api_key)
curl -fsS -H "Authorization: Bearer $VLLM_API_KEY" http://GPU_PRIVATE_IP:8001/v1/models
uv run python scripts/smoke_vllm.py --route standard --base-url http://GPU_PRIVATE_IP:8001 --model qwen3-1.7b
uv run python scripts/smoke_vllm.py --route complex  --base-url http://GPU_PRIVATE_IP:8002 --model qwen3-4b
```

**Check that the servers are not public**, from your laptop. This must fail:

```bash
curl --max-time 5 http://GPU_PUBLIC_IP:8001/health
```

**Stop the servers**, or shelve the instance to save allocation when nobody is using it:

```bash
docker compose -f compose.gpu.yml --env-file env/gpu.env -p smart-gateway-gpu down
```

`down` leaves `MODEL_CACHE_DIR` alone, so the next start skips the download.

## Tests and project layout

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src/
uv run pytest
cd frontend && npm test
```

The store tests in `tests/test_store.py` always run against the memory store, and also against Postgres when `TEST_DATABASE_URL` is set; otherwise those cases are skipped. CI sets it with a throwaway Postgres service. Each Postgres test uses its own new schema and drops it afterwards. To run them locally against a throwaway container:

```bash
docker run --rm -d --name gateway-test-db -p 127.0.0.1:55432:5432 \
  -e POSTGRES_PASSWORD=test postgres:18.6-alpine
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest
docker stop gateway-test-db
```

CI runs these checks, the frontend tests and build, a Compose smoke test through port 8080, and a check that Prometheus reports the gateway target as up. Frontend tests (Vitest and Testing Library, with `fetch` stubbed) cover the chat round trip, the details panel, error messages, and the Health screen in healthy and degraded states. Tests cover all ten intents, unknown messages, Jev failure and timeout, the rule router, backend fallback, label checks, the monitor and controller rules, request validation, policy grounding, settings overrides, bounded history, health responsiveness during simulated inference, the Prometheus metrics (values after requests, no message text, not served through Caddy) and the store contract.

- `src/app_assignment_1/`: settings, API contracts, routing interface, mock service, the monitor and controller, Prometheus metrics, and the request store (`store.py`, with SQL migrations in `migrations/`).
- `frontend/`: the React + Vite app (`src/components/ChatView.jsx`, `HealthView.jsx`, `Sidebar.jsx`) and its Dockerfile. Jev and Dashboard screens are listed as coming soon.
- `config/`: validated mock behavior, editable without Python changes.
- `Dockerfile` (gateway), `frontend/Dockerfile` (proxy and app), `compose.yml` (gateway, postgres, proxy, prometheus), `Caddyfile`, `prometheus/prometheus.yml`: deployment with one published port. `compose.gpu.yml` (vllm-small, vllm-large): the GPU host, bound to its private IP.
- `tests/`: unit and API tests. `scripts/smoke_compose.py`: public-proxy smoke test.
- `discussion.md`, `problem2-local-run.md`, benchmark results: historical Assignment 2 records. The original SmolLM2 implementation and measurement scripts remain in Git history before this prototype.

`Router` is a small async interface returning a validated `JevResult`: the route and intent answers, each checked on its own, plus the model and token counts. `TypeSafeJevRouter` implements it for real Jev (`APP_JEV_MODE=real`), and `VLLMBackend` implements `Backend` (returning intent, reply and facts) for real Qwen (`APP_PROVIDER_MODE=real`). `Store` works the same way for storage. The rest of the planned architecture is follow-up work.
