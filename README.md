# Assignment 1

DSAN 6700 (App Deployment) — Assignment 1.

The base repo for Machine Learning App Deployment course

It follows the instructions of using `uv` and the `src/` layout. It contains a
FastAPI service that uses Pydantic settings and models, with a `/predict`
endpoint backed by a local language model
([SmolLM2-135M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct))
running on CPU with PyTorch and Transformers.


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
uv sync --extra dev --frozen
```

This reads `uv.lock` and installs every dependency needed (check `uv.lock` for the list of dependencies).
The `dev` extra adds the tools the app itself does not need: pytest, ruff, mypy,
pre-commit. Omit `--extra dev` for a runtime-only install.

> The runtime dependencies include `torch` and `transformers`. On Linux the
> default `torch` wheel bundles CUDA libraries, so the first sync can download
> several GB.

### 4. Run the service

Start the live web server with uvicorn:

```bash
uv run uvicorn app_assignment_1.api:app --reload
```

- `app_assignment_1.api:app` points at the `app` object in
  [`src/app_assignment_1/api.py`](src/app_assignment_1/api.py).
- `--reload` auto-restarts the server when you edit code — a dev convenience;
  omit it in production.

**First run downloads the model.** At startup the service loads
SmolLM2-135M-Instruct once and reuses it for every request. The first time, the
weights (about 270 MB) are downloaded from Hugging Face — no account or token
needed — and cached under `~/.cache/huggingface` (set `HF_HOME` to move the
cache). Later starts load from the cache. The server only accepts requests
once the model has loaded; if the model cannot be loaded, the server logs the
error and exits.

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

### Run Problem 2 locally

Follow [the Problem 2 local run guide](problem2-local-run.md) to check Pydantic
Settings, run the real model, and reproduce the blocking and fixed benchmarks.

### Configure the service

Settings are read from `APP_*` environment variables (see
[Settings reference](#configuration-settings)). For local development, copy the
template and edit it:

```bash
cp env/.env.example .env
```

The deployed shape lives in `env/.env.production.example`.

### Ask the model a question

With the service running:

```bash
curl -X POST http://127.0.0.1:8000/predict \
  -H 'content-type: application/json' \
  -d '{"text":"What is a bank? Answer in one short sentence."}'
```

### Run the service with Docker

The service ships as a Docker image and is started with Docker Compose. You need
Docker with the Compose plugin (`docker compose version` should print a version).

**Start it** from the repository root:

```bash
docker compose up -d
```

That is the whole start procedure on a fresh clone. The first start builds the
image and downloads the model (~270 MB), so give it a minute or two.

**Check it is up:**

```bash
docker compose ps                          # STATUS shows (healthy) once ready
curl http://127.0.0.1:8000/health          # from the machine running it
curl http://<instance-ip>:8000/health    # our Jetstream2 instance, from anywhere
```

`<instance-ip>` is the instance's public IP, shown on its page in the Jetstream2 dashboard.

Expected response:

```json
{"status":"ok","version":"0.1.0","environment":"production"}
```

**Deploy a new version** (e.g. on Jetstream2 after a merge):

```bash
git pull
docker compose up -d --build
```

**Stop it:**

```bash
docker compose down        # keeps the downloaded model
```

> Do not add `-v` to `down` unless you want to delete the model volume; the
> next start would download the model again.

#### How the deployment is put together

- **One service, no dependencies.** The stack has no database, vector store, or
  cache; the model runs inside the app container.
- **Named volume for the model.** The `hf_vol` volume is mounted at `/models`
  (`HF_HOME`), so the downloaded model survives restarts and redeploys.
- **Health check.** The image declares a `HEALTHCHECK` that calls `/health`
  every 30 s, with a 120 s grace period while the model loads.
- **Non-root.** The container runs as an unprivileged `appuser`.
- **Restart policy.** `restart: unless-stopped` brings the service back after a
  crash or a reboot, unless it was stopped on purpose.
- **Configuration at run time.** Compose reads settings from
  `env/.env.production.example`; nothing is baked into the image, and
  `.dockerignore` keeps `.venv`, `.git`, and env files out of it.
  `APP_API_KEY` is validated but not yet used by any endpoint, so the
  template's placeholder is acceptable for this demo. A real deployment would
  supply the secret through a gitignored file instead.
- **CPU-only PyTorch on Linux.** Our Jetstream2 instance (`m3.small`) has no GPU
  and a 20 GB disk. The default Linux PyTorch pulls in ~2 GB of NVIDIA CUDA
  packages, which filled the disk, so `pyproject.toml` installs PyTorch's
  CPU-only build on Linux. macOS is unaffected.

Example response (decoding is greedy, so the same input gives the same answer):

```json
{"prediction":"A bank is a financial institution that provides a wide range of services, including lending, borrowing, and checking accounts.","input_length":45}
```

A single request took about 0.3 s of generation time on an Apple M4 CPU (see
[`discussion.md`](discussion.md)); expect different timings on other machines.
The separate [smoke check](benchmark_results/smoke_check.json) recorded about
6.6 s for one `/predict` request. Its hardware, warm-up state, and timing method
were not recorded, so that result cannot be directly compared with the 0.3 s
generation measurement. It confirms a working request, not typical latency.
Possible errors:

| Status | Body                                    | Meaning                                                     |
|--------|-----------------------------------------|-------------------------------------------------------------|
| `422`  | validation details                      | `text` is missing or empty.                                 |
| `500`  | `{"detail":"Model generation failed."}` | The model raised an error; the full error is in the server log. |
| `503`  | `{"detail":"Model not loaded."}`        | No model is attached to the app. A model load failure normally stops startup. |

To change the model or reply length, see `APP_LLM_MODEL_NAME` and
`APP_LLM_MAX_NEW_TOKENS` in the [settings reference](#configuration-settings).

### Add a dependency

```bash
uv add <package>                    # runtime dependency
uv add --optional dev <package>     # dev-only (tests, linters, types)
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
| POST   | `/predict` | `PredictRequest`  | `PredictResponse` | Text generation with SmolLM2.    |
| GET    | `/docs`    | —                 | HTML              | Auto-generated Swagger UI.       |

`PredictRequest` requires a non-empty `text`; invalid input returns HTTP `422`.
`/predict` returns `500` if generation fails and `503` if the model is not
loaded, both with an `ErrorResponse` body (`{"detail": "..."}`).

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
| `APP_LLM_MODEL_NAME` | str (Hugging Face model ID, non-empty)      | `HuggingFaceTB/SmolLM2-135M-Instruct` | Loaded once at startup.        |
| `APP_LLM_MAX_NEW_TOKENS` | int (1–512)                             | `64`          | Max tokens per reply; out-of-range values fail at startup. |

### Common commands

| Command                        | What it does                                        |
|--------------------------------|-----------------------------------------------------|
| `uv sync --extra dev --frozen` | Install runtime + dev extra from the lockfile.      |
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
├── discussion.md              # blocking-call audit write-up
├── benchmark_results/         # saved benchmark output (JSON)
├── scripts/
│   ├── benchmark_requests.py  # 10 concurrent /predict requests
│   └── measure_steps.py       # per-stage timing of one request
├── src/
│   └── app_assignment_1/
│       ├── __init__.py        # package marker + version
│       ├── api.py             # FastAPI app, lifespan, endpoints, Pydantic models
│       ├── config.py          # typed settings (Pydantic Settings)
│       └── model.py           # SmolLM2 loading and text generation
└── tests/
    └── test_api.py            # API tests with a fake model (no download)
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'torch'` (or `transformers`)

Your `.venv` was created before the model dependencies were added and is out of
date with `uv.lock`. The tests also need `torch`, even though they use a fake
model, because `api.py` imports `model.py`. Re-sync from the repo root:

```bash
uv sync --extra dev --frozen
```

### The server exits at startup with "Failed to load model"

The model could not be downloaded or loaded. Check your internet connection
(the first run downloads from Hugging Face), that `APP_LLM_MODEL_NAME` is a
valid model ID, and that the cache directory (`~/.cache/huggingface` or
`HF_HOME`) is writable. The full error is printed below the log line.

### `ModuleNotFoundError: No module named 'app_assignment_1'`

This means the **project package was not installed** into the environment you're
running. Because this repo uses a `src/` layout, `app_assignment_1` lives in
`src/app_assignment_1/` and is only importable once the project is *installed* —
it is never found just by being in the folder. The usual cause is building the
virtual environment by hand (e.g. `uv venv` + `uv pip install …`, or a conda env
with `pip install uvicorn`) instead of letting `uv sync` install the project.

**Fix — always set up with `uv sync`, from the repo root:**

```bash
cd "App - Assignment 1"
rm -rf .venv
uv sync --frozen
uv run uvicorn app_assignment_1.api:app --reload
```

The `uv sync` output must include the project itself being installed:

```
+ app-assignment-1==0.1.0 (from file:///.../App - Assignment 1)
```

**Check which problem you have:**

```bash
uv run python -c "import app_assignment_1; print(app_assignment_1.__file__)"
```

Prints a path → installed correctly. Still errors → the `rm -rf .venv && uv sync`
above fixes it.

**Quick workaround** (runs even without installing the project, by putting `src`
on the path):

```bash
uv run uvicorn --app-dir src app_assignment_1.api:app --reload
```

> The module name `app_assignment_1` comes from the package directory
> `src/app_assignment_1/`. The uvicorn target `app_assignment_1.api:app` means
> "import `src/app_assignment_1/api.py` and use its `app` object." Watch the
> spelling: assign**ment**, not "assignement".

---

## Acknowledgments

The initial project setup — `uv`/`src` scaffolding, the FastAPI skeleton,
Pydantic Settings configuration, the GitHub Actions CI workflow, and this
documentation — was done with the help of [Claude Code](https://claude.com/claude-code)
(Anthropic's Claude, Opus 4.8).
