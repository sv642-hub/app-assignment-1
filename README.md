# Assignment 1

DSAN 6700 (App Deployment) — Assignment 1.

The base repo for Machine Learning App Deployment course

It follows the instructions of using `uv` and the `src/` layout. It contains a
FastAPI skeleton, that uses Pydantic settings and models


- **[Installing](#installing)** — get it running from zero.
- **[How-to guides](#how-to-guides)** — task-focused recipes.
- **[Reference](#reference)** — endpoints, settings, commands, structure.

---

## Installing

How to install this repo on a new machine

### 1. Install `uv`

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then restart your shell (or `source` your profile) so `uv` is on your `PATH`.
Verify:

```bash
uv --version
```

### 2. Clone the repository

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git "App - Assignment 1"
cd "App - Assignment 1"
```

### 3. Create the environment from the lockfile

```bash
uv sync --frozen
```

This reads `uv.lock` and installs every dependency needed (check `uv.lock` for the list of dependencies)

### 4. Run the service

Start the live web server with uvicorn:

```bash
uv run uvicorn app_assignment_1.api:app --reload
```

- `app_assignment_1.api:app` points at the `app` object in
  [`src/app_assignment_1/api.py`](src/app_assignment_1/api.py).
- `--reload` auto-restarts the server when you edit code — a dev convenience;
  omit it in production.

Leave it running and, in a second terminal, confirm it's up:

```bash
curl http://127.0.0.1:8000/health
```

You should see:

```json
{"status":"ok","version":"0.1.0","environment":"local"}
```

> This runs the real service, not the test suite

### 5. Run the tests

```bash
uv run pytest
```

---

## How-to guides

### Configure the service

Settings are read from `APP_*` environment variables (see
[Settings reference](#configuration-settings)). For local development, copy the
template and edit it:

```bash
cp env/.env.example .env
```

The deployed shape lives in `env/.env.production.example`.

### Call the placeholder predict endpoint

```bash
curl -X POST http://127.0.0.1:8000/predict \
  -H 'content-type: application/json' \
  -d '{"text":"hello"}'
```

### Add a dependency

```bash
uv add <package>            # runtime dependency
uv add --dev <package>      # dev-only (tests, linters, types)
```

Both commands update `pyproject.toml` **and** `uv.lock`. Commit both so the team
stays in sync.

### Run the full CI check suite locally

These are the exact steps CI runs, in order:

```bash
uv sync --extra dev --frozen
uv run ruff check
uv run ruff format --check
uv run mypy src/
uv run pytest
```

Auto-fix formatting before committing:

```bash
uv run ruff format
uv run ruff check --fix
```

### Enable the pre-commit hooks (optional)

Runs ruff lint/format automatically on every commit:

```bash
uv run pre-commit install
```

---

## Reference

### Endpoints

| Method | Path       | Request body      | Response model    | Purpose                          |
|--------|------------|-------------------|-------------------|----------------------------------|
| GET    | `/health`  | —                 | `HealthResponse`  | Liveness check for callers/CI.   |
| POST   | `/predict` | `PredictRequest`  | `PredictResponse` | Placeholder echo "prediction".   |
| GET    | `/docs`    | —                 | HTML              | Auto-generated Swagger UI.       |

`PredictRequest` requires a non-empty `text`; invalid input returns HTTP `422`.

### Configuration (settings)

All variables use the `APP_` prefix and map to `Settings` in
[`src/app_assignment_1/config.py`](src/app_assignment_1/config.py).

| Variable          | Type / allowed values                          | Default       | Notes                                             |
|-------------------|------------------------------------------------|---------------|---------------------------------------------------|
| `APP_ENVIRONMENT` | `local` \| `staging` \| `production`           | `local`       | Enables stricter validators in `production`.      |
| `APP_DEBUG`       | bool                                           | `false`       | Must be `false` in production.                    |
| `APP_HOST`        | str                                            | `127.0.0.1`   | Use `0.0.0.0` inside a container.                 |
| `APP_PORT`        | int (1–65535)                                  | `8000`        | Out-of-range values fail at startup.              |
| `APP_LOG_LEVEL`   | `DEBUG`…`CRITICAL`                             | `INFO`        | Standard logging levels.                          |
| `APP_API_KEY`     | str                                            | `""`          | Required when `APP_ENVIRONMENT=production`.   |

### Common commands

| Command                        | What it does                                        |
|--------------------------------|-----------------------------------------------------|
| `uv sync --frozen`             | Install runtime + dev group from the lockfile.      |
| `uv run uvicorn app_assignment_1.api:app --reload` | Run the service with auto-reload.   |
| `uv run pytest`                | Run the test suite.                                 |
| `uv run ruff check`            | Lint.                                               |
| `uv run mypy src/`             | Type-check.                                         |

### Project structure

```
App - Assignment 1/
├── .github/workflows/ci.yml   # lint, format, type-check, test on push & PR
├── .pre-commit-config.yaml    # ruff lint/format git hooks
├── pyproject.toml             # single declarative config (deps, build, tools)
├── uv.lock                    # machine-generated lockfile — always committed
├── env/
│   ├── .env.example           # local-dev config template
│   └── .env.production.example # deployed config template
├── LICENSE · README.md
├── src/
│   └── app_assignment_1/
│       ├── __init__.py        # package marker + version
│       ├── api.py             # FastAPI app, endpoints, Pydantic models
│       └── config.py          # typed settings (Pydantic Settings)
└── tests/
    └── test_api.py
```