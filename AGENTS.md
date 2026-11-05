# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor) and for anyone pasting this into a chat.

## Project

Smart AI Gateway (DSAN 6700 final project): a FastAPI gateway that routes banking-support messages to a small or large Qwen model, monitors reply quality, and recovers on its own. Jev and the Qwen backends are currently mocked by JSON files in config/; real clients replace them behind the same interfaces.

## Layout

- `src/app_assignment_1/api.py`: FastAPI app and the `/v1/chat`, `/v1/requests/{id}` and `/v1/health` routes.
- `src/app_assignment_1/service.py`: gateway flow, reply format checks, in-memory request history.
- `src/app_assignment_1/router.py`: `Router` interface, `MockJevRouter`, and the fallback `RuleRouter`.
- `src/app_assignment_1/backends.py`: `Backend` interface and `MockBackend` (the mock Qwen backends).
- `src/app_assignment_1/intents.py`: keyword matching over the configured intents, shared by the mocks.
- `src/app_assignment_1/monitor.py`: in-memory failure monitor (format failures and failed calls) and `Controller`.
- `src/app_assignment_1/probe.py`: offline linear probe skeleton and gate check; not used by the gateway. See `docs/PROBE.md`.
- `src/app_assignment_1/schemas.py`: Pydantic API contracts and config models.
- `src/app_assignment_1/config.py`: cached Pydantic Settings.
- `src/app_assignment_1/__init__.py`: package version.
- `frontend/`: React (Vite) app with Chat and Health screens; `frontend/Dockerfile` builds it into the Caddy image.
- `config/`: `intents.json`, `policy_cards.json`, `backends.json`, plus `backends.chaos.json` and `backends.outage.json` for simulated faults.
- `tests/`: pytest suite. Frontend tests are in `frontend/src/App.test.jsx`.
- `scripts/smoke_compose.py`: smoke test against the public proxy.
- `env/`: `.env.example` and `.env.production.example` templates. A real `env/.env` is optional and ignored by git.
- `compose.yml`: `gateway` and `proxy` services. `compose.chaos.yml`: override that makes the small model corrupt every reply. `compose.outage.yml`: override that makes every call to the small model raise an error.
- `Caddyfile`: serves the React app on port 8080 and proxies `/v1`, `/docs`, `/redoc` and `/openapi.json` to the gateway.
- `docs/TEAM_PLAN.md`: who owns what. `USER_BRIEF.md`: guide for beta testers.

## Commands

Run from the repo root unless noted. CI uses Python 3.13 and Node 22.

```bash
# Install
uv sync --extra dev --frozen

# Python tests
uv run pytest

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
- Settings come from Pydantic Settings and env/ files. Never commit secrets or a real .env.
- Mocks sit behind interfaces (router, backends). Replace them by adding a real implementation, not by editing callers.
- Do not edit the frozen evaluation set or human grades once they exist.
- Docs use plain language. Do not claim results that have not been measured.
- Check docs/TEAM_PLAN.md for who owns an area before making large changes there; mention the owner in the PR.
