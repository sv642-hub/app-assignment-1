# App — Assignment 1

DSAN 6700 (App Deployment) — Assignment 1.

A minimal Python package using the **`src/` layout** with a single, shared,
reproducible environment managed by [`uv`](https://docs.astral.sh/uv/). Everyone
on the team installs the *exact same* dependency versions from the committed
`uv.lock`.

## Project structure

```
App - Assignment 1/
├── pyproject.toml            # single declarative config (deps, build, tools)
├── uv.lock                   # machine-generated lockfile — always committed
├── .python-version           # pinned interpreter for the team
├── .gitignore
├── .pre-commit-config.yaml    # ruff lint/format hooks
├── CHANGELOG.md
├── LICENSE
├── README.md
├── src/
│   └── app_assignment_1/
│       └── __init__.py
└── tests/
    └── test_greeting.py
```

## Getting started (each teammate)

You need `uv` installed. On macOS/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Clone the repository with remote tracking

```bash
git clone <REPO_URL> "App - Assignment 1"
cd "App - Assignment 1"
```

Cloning sets up the `origin` remote and a `main` branch that tracks
`origin/main` automatically. Verify:

```bash
git remote -v
git branch -vv
```

### 2. Create the shared environment from the lockfile

```bash
uv sync --extra dev
```

`uv sync` reads `uv.lock` and installs the **exact** recorded versions into a
local `.venv/` — no re-resolution — so every teammate, CI runner, and deployment
gets an identical environment.

### 3. Run it

```bash
uv run app-assignment-1
uv run pytest
```

## Working on the project

- **Add a runtime dependency:** `uv add <package>` (updates `pyproject.toml` and
  `uv.lock`). Commit both.
- **Add a dev-only dependency:** `uv add --dev <package>`.
- **Never edit `uv.lock` by hand.** Let `uv` regenerate it and commit the result
  so the team stays in sync.
- Run `pre-commit install` once to enable the lint hooks locally.
