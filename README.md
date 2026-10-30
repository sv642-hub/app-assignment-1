# Smart AI Gateway

A mocked banking-support gateway for DSAN 6700 Deliverable 2, with a React chat app in front of it. Send a customer message and get a reply from a fictional policy card, tagged `standard` or `complex`, with the intent and routing trace one click away. A Health screen shows the models and the self-healing controller.

**Everything is simulated.** There are no Jev calls, Qwen weights, GPU requirements, torch or Transformers dependencies. This prototype demonstrates the user journey in our planned architecture. It does not implement the full monitoring, self-healing, canary-release or linear-probe design.

## What happens to a message

1. The React app sends the message over HTTP to the FastAPI gateway.
2. `MockJevRouter` stands in for Jev's difficulty call. It returns only a route (`standard` or `complex`) and a confidence, never an intent. It uses the configured complexity of the best keyword match; unmatched messages go to `standard`.
3. Standard selects mock `qwen3-1.7b`. Complex selects mock `qwen3-4b`. The selected mock backend waits for its simulated delay, detects the intent by matching complete keyword phrases (longer/more matches score higher; config order breaks ties), and fills the reply template with fictional policy facts. Both mock backends share this classifier.
4. Reply checks reject blank text, replies over 4,000 characters, unfilled `{placeholder}` tokens and intent labels outside the configured list. Pydantic validates the result. The chat shows the reply and its route; **Show details** opens the intent, policy facts and trace.

If Jev fails or does not answer within `APP_JEV_TIMEOUT_SECONDS`, the rule router chooses the route instead: messages of `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` words or more go to `complex`, shorter ones to `standard`. The request still goes to a backend. The trace shows `router: "rule_router"`, `jev_choice: null`, `confidence: null` and `fallback_reason` `jev_timeout` or `jev_error`.

If the backend detects no intent, it returns intent `unknown` with a polite reply asking the user to rephrase (`reply_source: "no_match"`). If the selected backend is marked unavailable, the other is used only when available (`selected_backend_unavailable`). When both are unavailable, every message returns HTTP 503. When Jev fails and the backend falls back, `fallback_reason` lists both reasons.

Confidence is a keyword heuristic, not a calibrated probability. Measured latency includes the simulated wait. A failed format check returns a safe apology (`reply_source: "format_fallback"`) with `format_check: "failed"` and reasons in `format_errors`. The malformed reply is not returned or stored. A passed format check does not establish semantic correctness.

## Monitor and controller

The gateway runs a small in-memory self-healing loop, the first piece of the planned monitor:

- **Monitor:** for each backend, the gateway records whether each reply passed the format check, over the last `APP_MONITOR_WINDOW` replies (default 20).
- **Controller:** once a backend has at least `APP_MONITOR_MIN_SAMPLES` replies (default 5) and its format-failure rate reaches `APP_MONITOR_FAILURE_THRESHOLD` (default 0.5), the controller disables it for `APP_CONTROLLER_COOLDOWN_SECONDS` (default 60). Traffic for that route moves to the other backend with `fallback_reason: "selected_backend_disabled"`. After the cooldown, the backend is re-enabled with an empty window.
- **Action limit:** the controller never disables the last usable backend. It logs `kept_last_backend` instead and keeps serving.
- **Visibility:** the request that triggers an action shows it in `trace.controller_action` and in that reply's details. `GET /v1/health` lists samples, failures, failure rate and disabled status for each backend, the last 50 interventions with their reasons, and the controller rules. The **Health** screen shows the same data and refreshes every 5 seconds.

The monitor watches format failures only. It does not know whether an intent or reply is correct. Its state resets when the gateway restarts.

To see the loop work, start the stack with the chaos override. In `config/backends.chaos.json`, the small model has `fault_rate: 1`, so it corrupts every reply:

```bash
docker compose -f compose.yml -f compose.chaos.yml up -d --build --wait
```

Send five standard messages, such as "My card has not arrived". All five fail the format check, and the fifth disables `qwen3-1.7b`. The sixth is answered by `qwen3-4b`. Open the **Health** screen (`http://localhost:8080/#/health`) to see the model disabled and the intervention. Return to normal with `docker compose up -d --wait`.

## Run with Docker Compose

Requires Docker with Compose **2.24.0 or newer** for the optional env-file syntax. The clone commands use `main` and apply after this prototype PR is merged.

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git
cd app-assignment-1
docker compose up -d --build --wait
```

Open [the chat](http://localhost:8080) and click an example or type your own message, then press Enter. The **Health** screen is in the sidebar. [API documentation](http://localhost:8080/docs) and the [health endpoint](http://localhost:8080/v1/health) use the same port.

The stack has two services. `proxy` is Caddy (pinned to 2.11.7 and the tested multi-platform image digest) with the built React app baked in: [`frontend/Dockerfile`](frontend/Dockerfile) builds it with Node, then copies only the static files into the Caddy image. Caddy serves the app at `/` and proxies `/v1/*`, `/docs`, `/redoc` and `/openapi.json` to `gateway`. Only Caddy publishes a host port (`8080`); gateway port `8000` stays inside the Compose network. Both services restart unless stopped, and the proxy starts once the gateway is healthy.

```bash
docker compose ps
python3 scripts/smoke_compose.py http://localhost:8080
docker compose logs --tail=100
# Stop the prototype when finished:
docker compose down
```

Request history is in memory, bounded to the most recent 1,000 successful requests by default. It is lost when the gateway restarts. Run one gateway worker. No database or model volume is needed for this prototype. JSON configuration persists in the repository and is mounted read-only.

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
| `APP_POLICY_CARDS_PATH` | `config/policy_cards.json` | Fictional facts and `{facts}` reply templates |
| `APP_BACKENDS_PATH` | `config/backends.json` | Backend names, routes, delay and availability |
| `APP_REQUEST_STORE_LIMIT` | `1000` | Number of successful requests retained |
| `APP_JEV_TIMEOUT_SECONDS` | `2` | Wait for the Jev routing call before using the rule router |
| `APP_RULE_ROUTER_COMPLEX_MIN_WORDS` | `20` | Rule router sends messages this long or longer to `complex` |
| `APP_MONITOR_WINDOW` | `20` | Recent replies per backend that the monitor keeps |
| `APP_MONITOR_MIN_SAMPLES` | `5` | Replies needed before the controller may act; at most the window |
| `APP_MONITOR_FAILURE_THRESHOLD` | `0.5` | Format-failure rate that disables a backend |
| `APP_CONTROLLER_COOLDOWN_SECONDS` | `60` | How long a disabled backend stays out |

Paths are relative to the working directory: run native commands from the repo root. In Docker the working directory is `/app`; use paths under the mounted `config/` directory. The gateway validates JSON and matching intent/policy-card coverage at startup. Both mock backends must be configured. Set `available` to `false` in `config/backends.json` to try fallback.

Settings are cached and JSON files are loaded once at startup. After editing JSON files run `docker compose restart gateway`. After changing env values run `docker compose up -d --force-recreate`. Tests may call `get_settings.cache_clear()` to reread settings. Changing a model label does not run a real model.

Use public or fictional demo messages. The mock does not send messages to Jev or another model provider. It has no user authentication and stores responses in process memory.

## Run natively with uv

Use Python 3.13 as in CI. Install [uv](https://docs.astral.sh/uv/) first.

```bash
uv sync --extra dev --frozen
uv run uvicorn app_assignment_1.api:app --host 127.0.0.1 --port 8000
```

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
| `POST /v1/chat` | 200 validated reply; 422 invalid input; 503 both backends unavailable |
| `GET /v1/requests/{id}` | 200 stored response; 404 missing/evicted/restarted; 422 invalid UUID |
| `GET /v1/health` | Typed Pydantic response documented in OpenAPI; 200 with backend config, monitor statistics and recent interventions; `degraded` when any backend is unavailable or disabled by the controller |
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

CI runs these checks, the frontend tests and build, and a Compose smoke test through port 8080. Frontend tests (Vitest and Testing Library, with `fetch` stubbed) cover the chat round trip, the details panel, error messages, and the Health screen in healthy and degraded states. Tests cover all ten intents, unknown messages, Jev failure and timeout, the rule router, backend fallback, label checks, the monitor and controller rules, request validation, policy grounding, settings overrides, bounded history and health responsiveness during simulated inference.

- `src/app_assignment_1/`: settings, API contracts, routing interface, mock service, and the monitor and controller.
- `frontend/`: the React + Vite app (`src/components/ChatView.jsx`, `HealthView.jsx`, `Sidebar.jsx`) and its Dockerfile. Jev and Dashboard screens are listed as coming soon.
- `config/`: validated mock behavior, editable without Python changes.
- `Dockerfile` (gateway), `frontend/Dockerfile` (proxy and app), `compose.yml`, `Caddyfile`: deployment with one published port.
- `tests/`: unit and API tests. `scripts/smoke_compose.py`: public-proxy smoke test.
- `discussion.md`, `problem2-local-run.md`, benchmark results: historical Assignment 2 records. The original SmolLM2 implementation and measurement scripts remain in Git history before this prototype.

`Router` is a small async interface returning a validated `RoutingDecision` (route and confidence only). A future real Jev client can implement it, and a real vLLM client can implement `Backend` (returning intent, reply and facts), without changing the HTTP contract or the frontend. Persistent storage and the rest of the planned architecture are follow-up work.
