# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor) and for anyone pasting this into a chat.

## Project

Smart AI Gateway (DSAN 6700 final project): a FastAPI gateway that routes banking-support messages to a small or large Qwen model, monitors reply quality, and recovers on its own. Jev and the Qwen backends are currently mocked by JSON files in config/; real clients replace them behind the same interfaces. Request history and controller interventions go through a `Store`: in memory by default and in tests, Postgres in Compose.

## Layout

- `src/app_assignment_1/api.py`: FastAPI app, the `/v1/chat`, `/v1/requests/{id}` and `/v1/health` routes, and the internal `/metrics` route.
- `src/app_assignment_1/service.py`: gateway flow and reply format checks; saves requests and interventions to the store.
- `src/app_assignment_1/store.py`: `Store` interface, `MemoryStore` (default) and `PostgresStore` (psycopg 3, plain SQL), plus the migration runner.
- `src/app_assignment_1/migrations/`: numbered SQL files applied once at startup. Add a new file for schema changes; never edit one that has run.
- `src/app_assignment_1/router.py`: `Router` interface, `MockJevRouter`, and the fallback `RuleRouter`.
- `src/app_assignment_1/backends.py`: `Backend` interface and `MockBackend` (the mock Qwen backends).
- `src/app_assignment_1/intents.py`: keyword matching over the configured intents, shared by the mocks.
- `src/app_assignment_1/labels.py`: loads the shared labels (77 Banking77 intents plus `unknown`) and derives `option_set_version`.
- `src/app_assignment_1/monitor.py`: in-memory failure monitor (format failures and failed calls) and `Controller`; new interventions wait in `unsaved` until the gateway stores them.
- `src/app_assignment_1/records.py`: `JevAssessment` records for the semantic monitor, the `RecordSink` interface and an in-memory sink for tests; not written by the gateway yet.
- `src/app_assignment_1/metrics.py`: Prometheus metrics for the gateway, served at the internal `/metrics` path (not proxied by Caddy).
- `src/app_assignment_1/schemas.py`: Pydantic API contracts and config models.
- `src/app_assignment_1/config.py`: cached Pydantic Settings.
- `src/app_assignment_1/__init__.py`: package version.
- `frontend/`: React (Vite) app with Chat and Health screens; `frontend/Dockerfile` builds it into the Caddy image.
- `config/`: `labels.json` (the 78 labels Jev and Qwen classify into), `intents.json`, `policy_cards.json`, `backends.json`, plus `backends.chaos.json` and `backends.outage.json` for simulated faults.
- `tests/`: pytest suite. Frontend tests are in `frontend/src/App.test.jsx`.
- `scripts/smoke_compose.py`: smoke test against the public proxy.
- `scripts/render_architecture.py`: draws `docs/architecture.svg` from `docs/architecture.yaml`; `--check` fails if they differ.
- `env/`: `.env.example`, `.env.production.example` and `postgres.env.example` templates. Real `env/.env` and `env/postgres.env` are optional and ignored by git. `compose.defaults.env` holds public throwaway values (store and database password) so Compose and CI start without private files; never use it for a deployment.
- `compose.yml`: `gateway`, `postgres` (named volume `postgres-data`), `proxy` and `prometheus` services; only `proxy` publishes a port. `compose.chaos.yml`: override that makes the small model corrupt every reply. `compose.outage.yml`: override that makes every call to the small model raise an error.
- `Caddyfile`: serves the React app on port 8080 and proxies `/v1`, `/docs`, `/redoc` and `/openapi.json` to the gateway. Never add `/metrics`.
- `prometheus/prometheus.yml`: Prometheus scrape config; scrapes `gateway:8000/metrics` on the Compose network.
- `docs/TEAM_PLAN.md`: who owns what. `USER_BRIEF.md`: guide for beta testers.
- `docs/architecture.yaml`: what is built, simulated or planned in each part of the architecture, and who owns it. `docs/architecture.svg` is generated from it.

## Commands

Run from the repo root unless noted. CI uses Python 3.13 and Node 22.

```bash
# Install
uv sync --extra dev --frozen

# Python tests (Postgres store tests are skipped unless TEST_DATABASE_URL is set)
uv run pytest
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest

# Lint and type check
uv run ruff check
uv run ruff format --check
uv run mypy src/

# Pre-commit (ruff only; run mypy separately)
uv run pre-commit install
uv run pre-commit run --all-files

# Frontend install, tests and build
cd frontend
npm ci
npm test
npm run build
cd ..

# Run the stack
docker compose up -d --build --wait

# Redraw the architecture diagram after editing docs/architecture.yaml
uv run python scripts/render_architecture.py

# Smoke test the running stack
python3 scripts/smoke_compose.py http://localhost:8080

# Chaos mode or a simulated outage, then back to normal
docker compose -f compose.yml -f compose.chaos.yml up -d --build --wait
docker compose -f compose.yml -f compose.outage.yml up -d --build --wait
docker compose up -d --wait

# Stop the stack
docker compose down
```

## Rules

- Work on a branch and open a pull request; never push to main.
- Run the tests and pre-commit before committing; keep CI green.
- Only port 8080 (Caddy) is published. Never publish gateway, database, Prometheus or vLLM ports.
- Settings come from Pydantic Settings and env/ files. Never commit secrets or a real .env. Only `APP_` keys belong in `env/.env`; the Postgres container's password goes in `env/postgres.env`.
- Never store the customer's message text. `ChatResponse` has no message field; keep it that way.
- Mocks sit behind interfaces (router, backends). Replace them by adding a real implementation, not by editing callers.
- Do not edit the frozen evaluation set or human grades once they exist.
- Docs use plain language. Do not claim results that have not been measured.
- Check docs/TEAM_PLAN.md for who owns an area before making large changes there; mention the owner in the PR.
- If a PR changes what is built, simulated or owned, update `docs/architecture.yaml` in the same PR and run `scripts/render_architecture.py`. Mark what is true on main after the PR merges, not plans from other open PRs. Commit the YAML and the SVG together; CI fails if they differ.
