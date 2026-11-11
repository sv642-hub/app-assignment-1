# Smart AI Gateway

Who owns what: [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md) · What is built: [docs/architecture.svg](docs/architecture.svg) · Beta testers: [USER_BRIEF.md](USER_BRIEF.md)

A mocked banking-support gateway for DSAN 6700 Deliverable 2, with a React chat app in front of it. Send a customer message and get a reply from a fictional policy card, tagged `standard` or `complex`, with the intent and routing trace one click away. A Health screen shows the models and the self-healing controller.

**Everything is simulated.** There are no Jev calls, Qwen weights, GPU requirements, torch or Transformers dependencies. This prototype demonstrates the user journey in our planned architecture. It does not implement the full monitoring, self-healing, canary-release or linear-probe design.

## What happens to a message

1. The React app sends the message over HTTP to the FastAPI gateway.
2. `MockJevRouter` stands in for Jev's combined call, which answers two questions: the route (`standard` or `complex`) and the intent. It uses the configured complexity and intent of the best keyword match; unmatched messages go to `standard` with intent `unknown`. The gateway uses only the route answer. Jev's intent is an assessment for the monitor and never replaces the backend's intent. The gateway builds a record for each answer, but nothing stores the records yet.
3. Standard selects mock `qwen3-1.7b`. Complex selects mock `qwen3-4b`. The selected mock backend waits for its simulated delay, detects the intent by matching complete keyword phrases (longer/more matches score higher; config order breaks ties), and fills the reply template with fictional policy facts. Both mock backends share this classifier.
4. Reply checks reject blank text, replies over 4,000 characters, unfilled `{placeholder}` tokens and intent labels outside `config/labels.json`. Pydantic validates the result. The chat shows the reply and its route; **Show details** opens the intent, policy facts and trace.

If Jev fails or does not answer within `APP_JEV_TIMEOUT_SECONDS`, the rule router chooses the route instead: messages of `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` words or more go to `complex`, shorter ones to `standard`. The request still goes to a backend. The trace shows `router: "rule_router"`, `jev_choice: null`, `confidence: null` and `fallback_reason` `jev_timeout` or `jev_error`. Jev's answers are checked one at a time: an invalid route answer also uses the rule router, with `fallback_reason: "jev_invalid_route"`, while an invalid intent answer still uses Jev's route.

After `APP_JEV_BREAKER_FAILURES` failed Jev calls in a row (default 5), a circuit breaker skips Jev for `APP_JEV_BREAKER_COOLDOWN_SECONDS` (default 30), and the rule router routes every message with `fallback_reason: "jev_circuit_open"`. After the cool-off, one message tries Jev again: success closes the breaker, failure opens it for another cool-off. An invalid answer is not a failed call. `/v1/health` shows the breaker in `jev_breaker`. The breaker does not change `status`, which is about the backends only: while Jev is skipped, customers still get answers from the right models.

If the backend detects no intent, it returns intent `unknown` with a reply asking the customer to describe their banking problem (`reply_source: "no_match"`). A real backend must keep this behaviour. Vague and off-topic messages currently get the same reply. If a backend returns one of the 67 Banking77 intents that has no policy card, it must send an empty reply and no policy facts. The gateway then replies that the demo does not cover that topic (`reply_source: "unsupported_topic"`). The mock backends only match the 10 carded intents, so they never take this path. If the selected backend is marked unavailable, the other is used only when available (`selected_backend_unavailable`). When both are unavailable, every message returns HTTP 503. When Jev fails and the backend falls back, `fallback_reason` lists both reasons.

If the selected backend's call raises an error, or returns a reply that does not match the backend reply schema, the request is tried once on the other usable backend (`selected_backend_error`). Both cases count as failed calls (`call_errors`), not format failures. When no backend can answer, the message returns HTTP 503. The log records the backend and error type, not the message or the provider's error text. The gateway sets no time limit on a backend call yet, so a call that hangs is not treated as a failure; the real client must time out and raise.

Confidence is a keyword heuristic, not a calibrated probability. Measured latency includes the simulated wait. A failed format check returns a safe apology (`reply_source: "format_fallback"`) with `format_check: "failed"` and reasons in `format_errors`. The malformed reply is not returned or stored. A passed format check does not establish semantic correctness.

## Monitor and controller

The gateway runs a small in-memory self-healing loop, the first piece of the planned monitor:

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
- **Window:** the last `APP_SEMANTIC_WINDOW` samples per model (default 50). A model serving the other route's messages (after a fallback) sees different traffic, so those samples are counted apart, as `rerouted_agreements` and `rerouted_disagreements`.
- **Status** for each model, checked in this order:
  - `low_coverage`: at least `APP_SEMANTIC_MIN_SAMPLES` (default 10) samples, but fewer than `APP_SEMANTIC_MIN_COVERAGE` (default 0.5) had a Jev answer. During a Jev outage, missing data does not look like a healthy model.
  - `collecting`: fewer scored samples than the minimum.
  - `no_baseline`: no healthy baseline is set (`APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE`, unset by default), so rates are reported but never flagged.
  - `above_baseline`: the disagreement rate is above the baseline plus `APP_SEMANTIC_ALERT_MARGIN` (default 0.15).
  - `normal`: otherwise.
- **Visibility:** `GET /v1/health` has `semantic` (per model: counts, `disagreement_rate`, `coverage`, `status`) and `semantic_rules`. `/metrics` has `gateway_semantic_samples_total`, `gateway_semantic_disagreement_rate`, `gateway_semantic_coverage` and `gateway_semantic_above_baseline`. The rate and coverage gauges are absent while a model has no data, so an empty window never reads as 0%.

- **Semantic actions:** off by default (`APP_SEMANTIC_ACTIONS_ENABLED=false`), so the monitor only reports. When they are on and a model's status is `above_baseline`, the controller disables it, with the same `APP_CONTROLLER_COOLDOWN_SECONDS` and action limit as for format failures. The intervention reason gives the disagreement rate and the baseline. When the cooldown ends, the model is re-enabled with a fresh semantic window as well as a fresh failure window. Only the model's own window decides: rerouted replies never count toward disabling it. Low coverage and a missing baseline never cause an action.

The **Health** screen shows the monitor as a table, one row per model. While a model is above the baseline, a red banner says so, and says whether the controller acted. Keep semantic actions off until the baseline comes from dev data: a wrong baseline would take a healthy model out of service. The defaults are placeholders: the baseline, margin and coverage minimum should be set from dev data before the final test. In mock mode, Jev and both models share one keyword classifier, so a healthy mock always agrees and its baseline is 0. A disagreement rate measured on mocks says nothing about real model quality. The windows reset when the gateway restarts.

To see it work, use the label-swap override. In `config/backends.labelswap.json`, the small model has `label_swap_rate: 1`: it answers every matched message with the next intent's label and policy card. The override also sets the mock baseline of 0 and turns semantic actions on:

```bash
docker compose -f compose.yml -f compose.labelswap.yml up -d --build --wait
```

Send ten standard messages, such as "My card has not arrived". Each one gets a well-formed reply about a lost or stolen card, and every format check passes. The tenth puts `qwen3-1.7b` at a disagreement rate of 1.0, status `above_baseline`, and the controller disables it. The eleventh is answered correctly by `qwen3-4b`, with `fallback_reason: "selected_backend_disabled"`. The **Health** screen shows the banner, the disabled model and the intervention. To watch the monitor only report, add `APP_SEMANTIC_ACTIONS_ENABLED: "false"` to the override. Return to normal with `docker compose up -d --wait`.

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

- **Completed requests:** each `ChatResponse` (reply, intent, policy facts, trace), which `GET /v1/requests/{id}` and the Health screen's recent requests read. Only the most recent `APP_REQUEST_STORE_LIMIT` (default 1,000) are kept; older ones are deleted. Requests that end in a 503 are not stored.
- **Controller interventions:** every `disabled`, `re-enabled` and `kept_last_backend` entry. `GET /v1/health` shows the last 50.
- **Jev assessments:** two per request, one for the route question and one for the intent question, for the semantic monitor. They are saved in the same write as their request, so a request that ends in a 503 or is cancelled saves none. Postgres keeps them all, also after their request is deleted; the memory store keeps the last 2 × `APP_REQUEST_STORE_LIMIT`. Nothing reads them yet.

The customer's message is not stored: `ChatResponse` has no message field. The monitor's windows and which backends are disabled stay in process memory and reset on restart. The backend response records for the monitor are not stored yet; they will get their own store methods and migration once their schema is agreed.

There are two stores, chosen with `APP_STORE`:

- `memory` (the default, and what the tests use): process memory, lost when the gateway restarts.
- `postgres`: a Postgres database at `APP_DATABASE_URL`. Compose uses this. If `APP_STORE=postgres` and the URL is missing, the gateway refuses to start with a clear error.

If a write to the store fails or takes longer than `APP_STORE_TIMEOUT_SECONDS` (default 2), the customer still gets the reply and the gateway logs a warning with the error type only; that reply then cannot be looked up by id. A request makes at most two writes (interventions, then the request), so a database outage delays a reply by at most about twice that limit. Interventions that fail to save are retried on the next request, health check or shutdown, in their original order. If Postgres is unreachable, `GET /v1/health` and `GET /v1/requests/{id}` return an error.

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

Labels only take values from fixed lists. Message text, replies and request IDs are never labels; a test checks this. All values are in memory and reset when the gateway restarts. A re-enable is counted when the gateway next checks the model (a request, `/v1/health` or a scrape), not at the exact end of the cooldown. With the mock backends, latency includes the simulated delay and says nothing about real model speed. There are no token, cost or quality metrics yet, and Prometheus does not scrape vLLM or a separate monitor because neither exists yet.

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
| `APP_REQUEST_STORE_LIMIT` | `1000` | Number of successful requests retained, in memory or Postgres |
| `APP_JEV_TIMEOUT_SECONDS` | `2` | Wait for the Jev routing call before using the rule router |
| `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` | `20` | Rule router sends messages this long or longer to `complex` |
| `APP_MONITOR_WINDOW` | `20` | Recent calls per backend that the monitor keeps |
| `APP_MONITOR_MIN_SAMPLES` | `5` | Calls needed before the controller may act; at most the window |
| `APP_MONITOR_FAILURE_THRESHOLD` | `0.5` | Failure rate (failed format checks plus failed calls) that disables a backend |
| `APP_CONTROLLER_COOLDOWN_SECONDS` | `60` | How long a disabled backend stays out |
| `APP_STORE` | `memory` | `memory` (lost on restart) or `postgres`; Compose sets `postgres` |
| `APP_DATABASE_URL` | none | Postgres URL, required when `APP_STORE=postgres`; a secret, hidden in logs and settings output |
| `APP_STORE_TIMEOUT_SECONDS` | `2` | Longest wait for one store write before the reply goes out without it |
| `APP_PROVIDER_MODE` | `mock` | `mock` or `real`. Real mode needs `APP_JEV_API_KEY` and an `https` `APP_JEV_BASE_URL`, and is rejected at startup until the real clients exist |
| `APP_JEV_BASE_URL` | `https://api.typesafe.ai` | TypeSafe API for real Jev calls (not used yet) |
| `APP_JEV_API_KEY` | unset | Server-side TypeSafe key; set only in the real env file |
| `APP_JEV_MODEL` | `jev-1.14.0` | Pinned Jev version, not an alias such as `jev-latest` |
| `APP_JEV_INPUT_PRICE_PER_MILLION_USD` | unset | Jev input token price for cost estimates; Jev does not charge for output tokens. Cost stays null while unset |
| `APP_JEV_BREAKER_FAILURES` | `5` | Consecutive Jev failures before the circuit breaker skips Jev (not used yet) |
| `APP_JEV_BREAKER_COOLDOWN_SECONDS` | `30` | How long the breaker skips Jev (not used yet) |
| `APP_REQUEST_DEADLINE_SECONDS` | `45` | Total time for one chat request (not used yet) |
| `APP_ALTERNATE_RESERVE_SECONDS` | `15` | Part of the deadline kept for the alternate backend (not used yet); with the Jev timeout, must stay below the deadline |

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
| `POST /v1/chat` | 200 validated reply; 422 invalid input; 503 no backend available or able to answer |
| `GET /v1/requests/{id}` | 200 stored response; 404 missing, evicted, or lost in a restart with the memory store; 422 invalid UUID |
| `GET /v1/health` | Typed Pydantic response documented in OpenAPI; 200 with backend config, monitor statistics, recent interventions, recent requests (`recent_requests`) and per-model call totals (`usage`); `degraded` when any backend is unavailable or disabled by the controller |
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
docker compose -p smart-gateway-prototype up -d --build --wait
docker compose -p smart-gateway-prototype ps
curl -fsS http://127.0.0.1:8080/v1/health
python3 scripts/smoke_compose.py http://127.0.0.1:8080
```

Before the first start that includes Postgres, set your own database password as in [Storage](#storage); otherwise the stack runs with the public throwaway password.

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
- `Dockerfile` (gateway), `frontend/Dockerfile` (proxy and app), `compose.yml` (gateway, postgres, proxy, prometheus), `Caddyfile`, `prometheus/prometheus.yml`: deployment with one published port.
- `tests/`: unit and API tests. `scripts/smoke_compose.py`: public-proxy smoke test.
- `discussion.md`, `problem2-local-run.md`, benchmark results: historical Assignment 2 records. The original SmolLM2 implementation and measurement scripts remain in Git history before this prototype.

`Router` is a small async interface returning a validated `JevResult`: the route and intent answers, each checked on its own, plus the model and token counts. A future real Jev client can implement it, and a real vLLM client can implement `Backend` (returning intent, reply and facts), without changing the HTTP contract or the frontend. `Store` works the same way for storage. The rest of the planned architecture is follow-up work.
