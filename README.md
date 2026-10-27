# Smart AI Gateway (mocked prototype)

DSAN 6700 (App Deployment), final project, Deliverable 2.

The Smart AI Gateway sits in front of two LLMs for a banking support
assistant. For each customer message it asks a routing model (Jev) for the
intent and whether the request is "standard" (small model) or "complex" (large
model), sends it to that model, grounds the reply in a short policy card for
the intent, checks the reply's format, and returns the reply with a trace.

This prototype runs the whole path end to end with **mocks**: there is no real
Jev, no Qwen, and no GPU. Open the Playground, type a message, and you get:

- the detected **intent** (one of 10 Banking77 intents, or `unknown`),
- a **reply** grounded in that intent's policy card for a fictional bank,
  Harbor Point Bank,
- a **trace**: Jev's choice (`standard` or `complex`), which model answered,
  latency, and the format-check result.

- **[What is mocked](#what-is-mocked)**
- **[Run it locally](#run-it-locally)**: with [Docker Compose](#with-docker-compose-recommended) or [uv](#with-uv-no-docker)
- **[Deploy on Jetstream2](#deploy-on-jetstream2)**
- **[Call every endpoint](#call-every-endpoint)** with curl
- **[Reference](#reference)**: architecture, config files, settings, CI, structure

---

## What is mocked

| Real system (final project)            | This prototype                                                                 |
|----------------------------------------|--------------------------------------------------------------------------------|
| Jev, a hosted routing model            | `MockJevRouter`: keyword matching from [`config/intents.json`](config/intents.json) |
| Qwen3-1.7B and Qwen3-4B on vLLM        | `MockBackend`: waits a simulated latency from [`config/backends.json`](config/backends.json), then fills the policy card's reply template |
| Policy cards for a fictional bank      | Real (short) cards in [`config/policy_cards.json`](config/policy_cards.json), one per intent |
| Fallback rule router                   | A message with no intent match, or a router that raises, gets intent `unknown` and a polite fixed reply |
| Format checks                          | Reply is non-empty, at most 600 characters, and has no unfilled `{placeholders}` |
| Database for request logs              | In-memory store of the last 1,000 responses (lost on restart)                 |

What is real: the FastAPI gateway and its Pydantic-validated API, the Streamlit
Playground (which calls the gateway over HTTP only), the Caddy reverse proxy,
the Docker Compose stack, the tests, and CI.

**Swapping in the real Jev later.** The gateway depends only on the
`IntentRouter` interface in [`router.py`](src/smart_gateway/router.py): a
`name` and an async `route(message)` that returns `{route, intent,
confidence}`. A real Jev client implements the same method and replaces
`MockJevRouter` in [`api.py`](src/smart_gateway/api.py)'s lifespan; nothing
else changes. If it raises (timeout, outage), the gateway already falls back to
the rule router.

---

## Run it locally

### With Docker Compose (recommended)

You need Docker with the Compose plugin, **version 2.24.0 or newer** (`docker
compose version` to check).

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git
cd app-assignment-1
docker compose up -d --build
docker compose ps          # wait until gateway and frontend show (healthy)
```

Then open **http://localhost:8080** for the Playground, or
**http://localhost:8080/docs** for the API docs. Port 8080 (Caddy) is the only
published port; the gateway and Streamlit are reachable only inside the
Compose network.

Stop it with:

```bash
docker compose down
```

After editing anything in `config/`, rebuild: `docker compose up -d --build`.
The config files are copied into the gateway image.

### With uv (no Docker)

Install [uv](https://docs.astral.sh/uv/), then create the environment from the
lockfile:

```bash
uv sync --extra dev --extra playground --frozen
```

Start the gateway (terminal 1) and the Playground (terminal 2) from the repo
root, since the config paths are relative to it:

```bash
uv run uvicorn smart_gateway.api:app --reload
```

```bash
uv run streamlit run src/smart_gateway/playground.py
```

The gateway listens on http://127.0.0.1:8000 (docs at `/docs`) and the
Playground on http://localhost:8501. The Playground finds the gateway through
`PLAYGROUND_GATEWAY_URL` (default `http://127.0.0.1:8000`). There is no Caddy
in this mode.

Run the tests:

```bash
uv run pytest
```

---

## Deploy on Jetstream2

The stack runs on a CPU-only Jetstream2 instance (we use an `m3.small`: Ubuntu,
no GPU, 20 GB disk). The mocks need no GPU and no model download.

1. **Open port 8080.** In the Jetstream2 dashboard, the instance's security
   group must allow inbound TCP on port 8080. Port 8000 no longer needs to be
   open.
2. **Install Docker** with the Compose plugin (2.24.0 or newer), following
   [Docker's Ubuntu install guide](https://docs.docker.com/engine/install/ubuntu/),
   and enable it at boot so `restart: unless-stopped` brings the stack back:

   ```bash
   sudo systemctl enable --now docker
   ```

3. **Clone and start** (on the instance, over SSH):

   ```bash
   git clone https://github.com/sv642-hub/app-assignment-1.git
   cd app-assignment-1
   docker compose up -d --build
   docker compose ps                      # wait for (healthy)
   curl http://127.0.0.1:8080/v1/health
   ```

   If your user isn't in the `docker` group, prefix the `docker` commands with
   `sudo`.

4. **Open it from your own machine:** `http://<instance-ip>:8080` for the
   Playground. `<instance-ip>` is the public IP on the instance's page in the
   Jetstream2 dashboard.

**Upgrading an instance that ran Assignment 2.** The old stack had one service,
`app`, on port 8000 with the SmolLM2 model in a volume. Remove it and its large
image before building, so the 20 GB disk does not fill up:

```bash
cd app-assignment-1
docker compose down --remove-orphans
docker image rm app-assignment-1-app      # the old torch image (several GB)
docker volume rm app-assignment-1_hf_vol  # the SmolLM2 cache, no longer used
git pull
docker compose up -d --build
```

The production settings require `APP_API_KEY` (it is checked at startup but no
endpoint uses it yet). The committed template has a placeholder; to set a real
value, put it in `env/.env`, which is gitignored and overrides the template:

```bash
echo "APP_API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > env/.env
docker compose up -d
```

---

## Call every endpoint

Set `HOST` once. Every call goes through Caddy on port 8080:

```bash
export HOST=127.0.0.1:8080                 # local Docker Compose
# export HOST=<instance-ip>:8080           # Jetstream2
# export HOST=127.0.0.1:8000               # uv run uvicorn (no Caddy)
```

**`GET /v1/health`**: liveness check.

```bash
curl http://$HOST/v1/health
```

```json
{"status":"ok","version":"0.2.0","environment":"production","router":"mock-jev","intents":10,"backends":["qwen3-1.7b","qwen3-4b"]}
```

`environment` is `local` when running with `uv run` and the default settings.

**`POST /v1/chat`**: route a message and get the reply with its trace.

```bash
curl -X POST http://$HOST/v1/chat \
  -H 'content-type: application/json' \
  -d '{"message":"I think my card was stolen, what do I do?"}'
```

```json
{
  "request_id": "0c62c5977c9140319b2ec158374cd50b",
  "created_at": "2026-10-27T18:03:46.856331Z",
  "message": "I think my card was stolen, what do I do?",
  "intent": "lost_or_stolen_card",
  "reply": "I'm sorry to hear about your card. Freeze the card right away in the app under Cards > Freeze. Report it as lost or stolen so the card is cancelled for good. A replacement card ships for free within 5-7 business days. Charges made after you report the card are not your responsibility.",
  "policy_card": {
    "intent": "lost_or_stolen_card",
    "title": "Lost or stolen cards",
    "facts": [
      "Freeze the card right away in the app under Cards > Freeze.",
      "Report it as lost or stolen so the card is cancelled for good.",
      "A replacement card ships for free within 5-7 business days."
    ]
  },
  "trace": {
    "router": "mock-jev",
    "route": "complex",
    "confidence": 1.0,
    "model": "qwen3-4b",
    "latency_ms": 385.3,
    "format_check": "pass",
    "fallback_reason": null
  }
}
```

`latency_ms` is mostly the simulated backend latency: about 120 ms for
`qwen3-1.7b` and 380 ms for `qwen3-4b`.

**`POST /v1/chat` with an unknown message**: the rule router answers.

```bash
curl -X POST http://$HOST/v1/chat \
  -H 'content-type: application/json' \
  -d '{"message":"Can you recommend a good pizza place?"}'
```

```json
{"request_id":"5c296f5f29ee473fa962690a250e5ee3","created_at":"2026-10-27T18:03:46.995225Z","message":"Can you recommend a good pizza place?","intent":"unknown","reply":"Thanks for contacting Harbor Point Bank. I couldn't match your message to a topic I can help with yet. Could you rephrase it, or reach a support agent in the app under Help > Chat with us?","policy_card":null,"trace":{"router":"rule-router","route":"standard","confidence":0.0,"model":null,"latency_ms":0.2,"format_check":"pass","fallback_reason":"no_intent_match"}}
```

**`POST /v1/chat` with invalid input**: an empty, whitespace-only, missing, or
over-2,000-character `message` is rejected with `422`.

```bash
curl -i -X POST http://$HOST/v1/chat \
  -H 'content-type: application/json' \
  -d '{"message":""}'
```

**`GET /v1/requests/{id}`**: look up a recent response by its `request_id`.

```bash
ID=$(curl -s -X POST http://$HOST/v1/chat -H 'content-type: application/json' \
  -d '{"message":"My top up failed"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')
curl http://$HOST/v1/requests/$ID
```

An unknown id returns `404` with `{"detail":"Request not found."}`. The store
is in memory, so ids do not survive a gateway restart.

**`GET /docs`, `GET /redoc`, `GET /openapi.json`**: the API docs and schema.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://$HOST/docs     # prints 200
curl -s -o /dev/null -w '%{http_code}\n' http://$HOST/redoc    # prints 200
curl http://$HOST/openapi.json
```

**`GET /`**: the Playground. Open `http://$HOST/` in a browser.

---

## Reference

### Architecture

```
browser ──► :8080  proxy (Caddy) ──┬── /v1/*, /docs*, /redoc, /openapi.json ──► gateway:8000 (FastAPI)
                                   └── everything else ──────────────────────► frontend:8501 (Streamlit)
                                                                                     │
                                       frontend calls gateway over HTTP ◄────────────┘
                                       (PLAYGROUND_GATEWAY_URL=http://gateway:8000)
```

Inside the gateway, `POST /v1/chat` runs:

1. **Route.** `MockJevRouter` scores each intent's keywords in the message
   (each match counts its word count). The best intent wins; its `complexity`
   is the route and its share of the total score is the confidence.
2. **Answer.** The backend for that route (`standard` → `qwen3-1.7b`,
   `complex` → `qwen3-4b`) waits its simulated latency and fills the intent's
   policy card template with its facts.
3. **Fallback.** No keyword match, or a router exception, skips the backend
   call: intent `unknown`, `router: "rule-router"`, `model: null`, and
   the polite `fallback_reply` from the policy cards file.
4. **Check and record.** The format check runs on the reply, the trace is
   assembled, and the response is stored for `GET /v1/requests/{id}`.

### Config files

All three are validated with Pydantic at startup, and checked against each
other: every intent needs a policy card, and every route used by an intent
needs exactly one backend. A broken file stops the gateway from starting.

| File                                                    | Contents                                                                 |
|---------------------------------------------------------|--------------------------------------------------------------------------|
| [`config/intents.json`](config/intents.json)            | 10 Banking77 intents, each with `keywords` and a mock `complexity` (`standard` or `complex`) |
| [`config/policy_cards.json`](config/policy_cards.json)  | `bank_name`, `fallback_reply`, and one card per intent: `title`, 3 `facts`, and a `reply_template` containing `{facts}` |
| [`config/backends.json`](config/backends.json)          | `qwen3-1.7b` (route `standard`, 120 ms) and `qwen3-4b` (route `complex`, 380 ms) |

### Configuration settings

The gateway reads `APP_*` variables into `Settings` in
[`src/smart_gateway/config.py`](src/smart_gateway/config.py). An unknown key
in a `.env` file is rejected at startup.

| Variable                  | Default                       | Notes                                              |
|---------------------------|-------------------------------|----------------------------------------------------|
| `APP_ENVIRONMENT`         | `local`                       | `local`, `staging`, or `production`                |
| `APP_DEBUG`               | `false`                       | Must be `false` in production                      |
| `APP_HOST` / `APP_PORT`   | `127.0.0.1` / `8000`          |                                                    |
| `APP_LOG_LEVEL`           | `INFO`                        |                                                    |
| `APP_API_KEY`             | empty                         | Required in production                             |
| `APP_INTENTS_PATH`        | `config/intents.json`         | Must exist; relative to the working directory      |
| `APP_POLICY_CARDS_PATH`   | `config/policy_cards.json`    | Must exist                                         |
| `APP_BACKENDS_PATH`       | `config/backends.json`        | Must exist                                         |
| `APP_REQUEST_STORE_SIZE`  | `1000`                        | Recent responses kept for `/v1/requests/{id}`      |

Templates: [`env/.env.example`](env/.env.example) (local) and
[`env/.env.production.example`](env/.env.production.example) (what Compose
loads). `uv run uvicorn` reads `.env` at the repo root; Compose reads
`env/.env`. Both are gitignored.

The Playground reads `PLAYGROUND_GATEWAY_URL` (default
`http://127.0.0.1:8000`) and `PLAYGROUND_TIMEOUT_SECONDS` (default `10`).

### Docker

- **One Dockerfile, two targets.** `gateway` installs only the core
  dependencies (FastAPI, Pydantic Settings, uvicorn; no torch or
  transformers). `frontend` adds the `playground` extra (Streamlit, httpx).
- **Health checks.** The gateway image checks `/v1/health` and the frontend
  image checks Streamlit's `/_stcore/health`. Compose starts the frontend once
  the gateway is healthy, and the proxy once both are.
- **Non-root.** Both containers run as an unprivileged `appuser`.
- **Restart policy.** `restart: unless-stopped` on all three services.
- **Image sizes** (local build, arm64): gateway about 314 MB, frontend about
  812 MB (Streamlit brings pandas and pyarrow), `caddy:2.10-alpine` about 80 MB.

### CI and local checks

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same steps
on every push and pull request:

```bash
uv sync --extra dev --extra playground --frozen
uv run ruff check
uv run ruff format --check
uv run mypy src/
uv run pytest
```

Tests cover routing for every intent, the fallback paths (no match and router
error), the `/v1/chat` response schema and input validation, request lookup,
health, the docs, config-file validation, and the Playground page (through
Streamlit's `AppTest`, against the real gateway app in-process).

### Add a dependency

```bash
uv add <package>                           # gateway runtime dependency
uv add --optional playground <package>     # Playground only
uv add --optional dev <package>            # tests, linters, types
```

Commit both `pyproject.toml` and `uv.lock`.

### Project structure

```
config/                  intents, policy cards, mock backends (JSON)
src/smart_gateway/
  api.py                 FastAPI app: /v1/chat, /v1/requests/{id}, /v1/health
  gateway.py             request pipeline, format check, in-memory store
  router.py              IntentRouter interface and MockJevRouter
  backends.py            MockBackend (simulated Qwen)
  catalog.py             loads and cross-checks the config files
  schemas.py             Pydantic request/response models
  config.py              gateway Settings (APP_*)
  client.py              Playground HTTP client and settings (PLAYGROUND_*)
  playground.py          Streamlit page
tests/                   pytest suite
Caddyfile                proxy routes
Dockerfile               gateway and frontend targets
compose.yml              proxy, frontend, gateway
env/                     settings templates
archive/assignment-2/    Assignment 2 benchmarks, scripts, and write-ups (SmolLM2 service, kept for the record)
```

The files in `archive/assignment-2/` describe the earlier SmolLM2 service; its
scripts target the removed `/predict` endpoint and no longer run against this
code.
