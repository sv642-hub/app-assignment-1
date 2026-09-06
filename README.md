# App — Assignment 1

DSAN 6700 (App Deployment) — Assignment 1.

A minimal but *real* FastAPI web service, packaged with the **`src/` layout** and
a single, shared, reproducible environment managed by
[`uv`](https://docs.astral.sh/uv/). Everyone on the team installs the exact same
dependency versions from the committed `uv.lock`.

This README is organized along the [Diátaxis](https://diataxis.fr/) framework:

- **[Tutorial](#tutorial-run-it-on-a-clean-machine)** — get it running from zero.
- **[How-to guides](#how-to-guides)** — task-focused recipes.
- **[Reference](#reference)** — endpoints, settings, commands, structure.
- **[Explanation](#explanation)** — why the project is built this way.

---

## Tutorial: run it on a clean machine

Start-to-finish on a machine with nothing installed but `git`. Copy-paste each block.

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
uv sync --extra dev
```

This reads `uv.lock` and installs the **exact** recorded versions into a local
`.venv/`. `uv` downloads the pinned Python (3.13) automatically if you don't have
it — you do not need to install Python yourself.

### 4. Run the service

```bash
uv run uvicorn app_assignment_1.api:app --reload
```

Leave it running and, in a second terminal, confirm it's up:

```bash
curl http://127.0.0.1:8000/health
```

You should see:

```json
{"status":"ok","version":"0.1.0","environment":"local"}
```

Open the interactive API docs in a browser at
<http://127.0.0.1:8000/docs>.

### 5. Run the checks

```bash
uv run pytest
```

That's the whole loop: install `uv`, clone, `uv sync`, run. You now have the
same environment as every teammate and as CI.

---

## How-to guides

### Configure the service

Settings are read from `APP_*` environment variables (see
[Settings reference](#configuration-settings)). For local development, copy the
template and edit it:

```bash
cp .env.example .env
```

`.env` is git-ignored and never committed. The deployed shape lives in
`.env.production.example`.

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
| `APP_API_KEY`     | str                                            | `""`          | **Required** when `APP_ENVIRONMENT=production`.   |

### Common commands

| Command                        | What it does                                        |
|--------------------------------|-----------------------------------------------------|
| `uv sync --extra dev`          | Install runtime + dev deps from the lockfile.       |
| `uv run uvicorn app_assignment_1.api:app --reload` | Run the service with auto-reload.   |
| `uv run pytest`                | Run the test suite.                                 |
| `uv run ruff check`            | Lint.                                               |
| `uv run mypy src/`             | Type-check.                                         |

### Project structure

```
App - Assignment 1/
├── .github/workflows/ci.yml   # lint, format, type-check, test on push & PR
├── pyproject.toml             # single declarative config (deps, build, tools)
├── uv.lock                    # machine-generated lockfile — always committed
├── .python-version            # pinned interpreter for the team (3.13)
├── .env.example               # local-dev config template
├── .env.production.example    # deployed config template
├── .pre-commit-config.yaml    # ruff lint/format hooks
├── CHANGELOG.md · LICENSE · README.md
├── src/
│   └── app_assignment_1/
│       ├── __init__.py        # package marker + version
│       ├── api.py             # FastAPI app, endpoints, Pydantic models
│       └── config.py          # typed settings (Pydantic Settings)
└── tests/
    └── test_api.py
```

---

## Explanation

### Why the `src/` layout

With a flat layout, Python imports the local package before the installed one
when you run `pytest` at the repo root, so tests can pass locally yet fail in
CI (the "phantom import"). Placing the package under `src/` makes the local
source invisible to the import system, so tests always run against the
*installed* package — your dev environment behaves like a user's.

### Why a committed `uv.lock`

`uv sync` installs the exact dependency tree recorded in `uv.lock` without
re-resolving. Committing the lockfile means every teammate, every CI run, and
every deployment gets byte-for-byte identical dependencies — no "works on my
machine." CI uses `uv sync --frozen` to *fail* if the lockfile and
`pyproject.toml` ever drift apart.

### Why fail-fast configuration

Configuration is validated by Pydantic Settings at startup. Bad config (a port
out of range, production without an API key, a typo'd key in `.env`) raises
immediately and stops the process, instead of letting the service limp along in
a broken state and fail mysteriously later. Real secrets live in a git-ignored
`.env` or the deployment platform's secret store — never in the repository.
