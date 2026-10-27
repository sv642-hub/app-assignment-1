# Smart AI Gateway

A mocked banking-support Playground for DSAN 6700 Deliverable 2. Enter a customer message and see a detected intent, a reply from a fictional policy card, and a routing trace.

**Everything is simulated.** There are no Jev calls, Qwen weights, GPU requirements, torch or Transformers dependencies. This prototype demonstrates the user journey in our planned architecture. It does not implement the full monitoring, self-healing, canary-release or linear-probe design.

## What happens to a message

1. Streamlit sends the message over HTTP to the FastAPI gateway.
2. `MockJevRouter` matches complete keyword phrases against ten configured intents. Longer/more matching phrases score higher. Config order breaks ties. The configured complexity selects `standard` or `complex`.
3. Standard selects mock `qwen3-1.7b`. Complex selects mock `qwen3-4b`. The service waits for the configured simulated delay and fills the reply template with fictional policy facts.
4. Pydantic validates the result. The Playground shows the intent, reply and trace.

Unknown messages use the rule router with intent `unknown`, a standard route and a polite request to rephrase. There is no fabricated Jev choice for unknown messages: `jev_choice` is `null`. If a selected mock backend is marked unavailable, the other is used only when available. Both unavailable returns HTTP 503. The trace preserves the initial Jev choice and identifies the final route.

Confidence is a keyword heuristic, not a calibrated probability. Measured latency includes the simulated wait. A passed format check means the result has the required structure; it does not establish semantic correctness.

## Run with Docker Compose

Requires Docker with Compose **2.24.0 or newer** for the optional env-file syntax.

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git
cd app-assignment-1
# Until this PR is merged:
git switch feat/smart-gateway-mocked-prototype
docker compose up -d --build --wait
```

Open [the Playground](http://localhost:8080) and click an example or type your own message. Click **Send message**. [API documentation](http://localhost:8080/docs) and [health](http://localhost:8080/v1/health) use the same port.

Only Caddy publishes a host port (`8080`). Gateway port `8000` and Streamlit port `8501` stay inside the Compose network. Caddy proxies Streamlit WebSockets as well as HTTP. The three services restart unless stopped. Frontend startup waits for gateway health; proxy startup waits for both services.

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

Pydantic Settings reads `.env` then `env/.env` in native local runs. Process environment variables take precedence. Compose injects `env/.env` at runtime; no real env file is copied into the image. Compose explicitly sets the frontend gateway URL to `http://gateway:8000` so it reaches the internal service.

| Setting | Default | Purpose |
| --- | --- | --- |
| `APP_ENVIRONMENT` | `local` | `local`, `staging` or `production` |
| `APP_INTENTS_PATH` | `config/intents.json` | Intent keywords and mock complexity |
| `APP_POLICY_CARDS_PATH` | `config/policy_cards.json` | Fictional facts and `{facts}` reply templates |
| `APP_BACKENDS_PATH` | `config/backends.json` | Backend names, routes, delay and availability |
| `APP_GATEWAY_URL` | `http://127.0.0.1:8000` | Frontend's HTTP gateway address |
| `APP_REQUEST_TIMEOUT_SECONDS` | `15` | Frontend HTTP timeout |
| `APP_REQUEST_STORE_LIMIT` | `1000` | Number of successful requests retained |

Paths are relative to the working directory: run native commands from the repo root. In Docker the working directory is `/app`; use paths under the mounted `config/` directory. The gateway validates JSON and matching intent/policy-card coverage at startup. Both mock backends must be configured. Set `available` to `false` in `config/backends.json` to try fallback.

Settings are cached and JSON files are loaded once at startup. After editing JSON files run `docker compose restart gateway`. After changing env values run `docker compose up -d --force-recreate`. Tests may call `get_settings.cache_clear()` to reread settings. Changing a model label does not run a real model.

Use public or fictional demo messages. The mock does not send messages to Jev or another model provider. It has no user authentication and stores responses in process memory. Streamlit usage telemetry is disabled in the documented commands.

## Run natively with uv

Use Python 3.13 as in CI. Install [uv](https://docs.astral.sh/uv/) first.

```bash
uv sync --extra dev --frozen
uv run uvicorn app_assignment_1.api:app --host 127.0.0.1 --port 8000
```

In another terminal at the repo root:

```bash
uv run streamlit run src/app_assignment_1/playground.py --server.address=127.0.0.1 --server.port=8501 --browser.gatherUsageStats=false
```

Open `http://localhost:8501`. Native development uses separate frontend and API ports. The Compose path above is the single-port deployment.

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
    "confidence": 0.87,
    "latency_ms": 121.4,
    "simulated_latency_ms": 120,
    "format_check": "passed",
    "fallback_reason": null,
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
| `GET /v1/health` | 200 with mock backend availability; `degraded` when both disabled |
| `GET /docs`, `/redoc`, `/openapi.json` | Interactive documentation and API schema |

Messages must contain 1–2,000 characters and cannot be whitespace-only. Extra request fields are rejected.

## Deploy on Jetstream2

Unshelve the instance and confirm its current public IP first. The last recorded address was `149.165.147.167`; verify it has not changed. SSH username is `exouser`. The following assumes Docker with Compose 2.24+ is already installed and your account can run Docker.

From your laptop:

```bash
ssh exouser@149.165.147.167
```

On the instance, use a separate directory so the earlier assignment checkout is preserved:

```bash
df -h /
git clone --branch feat/smart-gateway-mocked-prototype --single-branch \
  https://github.com/sv642-hub/app-assignment-1.git smart-ai-gateway-prototype
cd smart-ai-gateway-prototype
docker compose version
docker compose -p smart-gateway-prototype up -d --build --wait
docker compose -p smart-gateway-prototype ps
curl -fsS http://127.0.0.1:8080/v1/health
python3 scripts/smoke_compose.py http://127.0.0.1:8080
```

After the PR merges, new deployments can clone `main` instead. For updates to this branch:

```bash
cd ~/smart-ai-gateway-prototype
git pull --ff-only
docker compose -p smart-gateway-prototype up -d --build --wait
```

Allow inbound TCP **8080** in the instance's security group and host firewall. Keep the existing SSH access rule. Do not publish gateway, Streamlit or database ports. If 8080 is already occupied, identify that service before stopping anything.

Back on your laptop, verify external access:

```bash
curl -fsS http://149.165.147.167:8080/v1/health
curl -fsS http://149.165.147.167:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"My card has not arrived"}'
```

Open `http://149.165.147.167:8080` and send a Playground message. Leave Compose running for grading. Local verification does not confirm instance deployment or external reachability.

## Tests and project layout

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src/
uv run pytest
```

CI runs these checks and a Compose smoke test through port 8080. Tests cover all ten intents, unknown messages, router failure, backend fallback, request validation, policy grounding, settings overrides, bounded history and health responsiveness during simulated inference.

- `src/app_assignment_1/`: settings, API contracts, routing interface, mock service and Playground.
- `config/`: validated mock behavior, editable without Python changes.
- `Dockerfile`, `compose.yml`, `Caddyfile`: deployment with one published port.
- `tests/`: unit and API tests. `scripts/smoke_compose.py`: public-proxy smoke test.
- `discussion.md`, `problem2-local-run.md`, benchmark results: historical Assignment 2 records. The original SmolLM2 implementation and measurement scripts remain in Git history before this prototype.

`Router` is a small async interface returning a validated `RoutingDecision`. A future real Jev client can implement it without changing the HTTP contract or Playground. Persistent storage and the rest of the planned architecture are follow-up work.
