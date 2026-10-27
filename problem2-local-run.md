> Historical Assignment 2 documentation. These commands target the pre-prototype service. Use the current README for the mocked gateway; use Git history for the original model and benchmarks.

# Run Problem 2 locally

This guide runs the real SmolLM2 model, checks Pydantic Settings and environment
configuration, and reproduces the blocking-call comparison described in
[discussion.md](discussion.md).

Use a macOS or Linux terminal with Git and `uv` installed. Run the commands from
the repository root, where `pyproject.toml` and `uv.lock` are located.

## 1. Install dependencies and prepare local settings

```bash
uv sync --extra dev --frozen
test -f .env || cp env/.env.example .env
```

The second command preserves an existing `.env`. Edit that file so these entries
have the following values:

```dotenv
APP_ENVIRONMENT=local
APP_DEBUG=false
APP_LLM_MODEL_NAME=HuggingFaceTB/SmolLM2-135M-Instruct
APP_LLM_MAX_NEW_TOKENS=64
```

Environment variables already set in your terminal override `.env`. Check the
values Pydantic actually reads without printing an API key:

```bash
uv run --no-sync python -c 'from app_assignment_1.config import get_settings; s = get_settings(); print({"environment": s.environment, "debug": s.debug, "model": s.llm_model_name, "max_new_tokens": s.llm_max_new_tokens})'
```

Expected values are `local`, `False`, `HuggingFaceTB/SmolLM2-135M-Instruct`, and
`64`. Settings are cached for the running process; restart the server after
changing them.

## 2. Run the tests and checks

```bash
uv run --no-sync pytest
uv run --no-sync ruff check
uv run --no-sync ruff format --check
uv run --no-sync mypy src/
```

The API tests use fake generators. They check settings passed to the model,
single loading, request validation, and failure responses, without downloading
model weights. The following steps exercise the real model.

To confirm that a terminal environment variable overrides `.env`:

```bash
APP_LLM_MAX_NEW_TOKENS=16 uv run --no-sync python -c 'from app_assignment_1.config import get_settings; print(get_settings().llm_max_new_tokens)'
```

This should print `16`. To check invalid configuration:

```bash
APP_LLM_MAX_NEW_TOKENS=0 uv run --no-sync python -c 'from app_assignment_1.config import get_settings; get_settings()'
```

The second command is expected to fail with a Pydantic validation error. These
inline overrides apply only to their individual commands. The allowed token
limit is 1–512; an empty model name is rejected. Production settings require a
nonempty API key and debug disabled.

## 3. Start the real model API

In terminal 1:

```bash
uv run --no-sync uvicorn app_assignment_1.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Wait for `Application startup complete.` The first start may download the model
from Hugging Face. Later starts can reuse its cache. Model loading happens once
per application process. If loading fails, startup stops.

## 4. Submit requests

In terminal 2, enter the same repository folder and run:

```bash
curl http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{"text":"What is a bank? Answer in one short sentence."}'
```

Expect HTTP 200. The health response should report `environment: local`. The
prediction response contains generated text in `prediction` and
`input_length: 45`. Exact wording and response time can vary.

Check request validation:

```bash
curl -i -X POST http://127.0.0.1:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{"text":""}'
```

Expect HTTP 422. Generated replies demonstrate the model request path; they do
not establish banking-policy correctness.

## 5. Run the fixed-version benchmark

Keep terminal 1's server running. In terminal 2:

```bash
uv run --no-sync python scripts/benchmark_requests.py fixed
```

The script sends one excluded warm-up request, then ten concurrent requests. It
prints each request's elapsed time, total wall-clock time, and the slowest
request. Results are written to `benchmark_results/fixed.json`.

The script replaces an existing result file. Copy saved evidence elsewhere
before running if you want to preserve it.

Stop the server in terminal 1 with Ctrl+C when finished.

## 6. Measure the processing stages

With the server stopped, run from the repository root:

```bash
uv run --no-sync python scripts/measure_steps.py
```

This loads the real model, performs an excluded warm-up, and prints timings for
validation, generation, and response construction/serialization. The script
uses SmolLM2 and 64 output tokens explicitly; it does not read the model settings
from `.env`. These timings exclude model loading, HTTP transport, endpoint
scheduling, and queueing.

## 7. Reproduce the older blocking version

Stop any server using port 8000. From the current repository root, create a
separate checkout for the saved blocking tag:

```bash
git fetch origin --tags
git worktree add --detach ../app-assignment-1-blocking problem2-blocking-v1
cd ../app-assignment-1-blocking
uv sync --extra dev --frozen
uv run --no-sync uvicorn app_assignment_1.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Use a different sibling folder name if `../app-assignment-1-blocking` already
exists. The historical code uses explicit SmolLM2/64-token settings.

In another terminal, enter that blocking checkout and run:

```bash
uv run --no-sync python scripts/benchmark_requests.py blocking
```

The output is `benchmark_results/blocking.json` in the blocking checkout. Stop
that server with Ctrl+C after the run. Keep the fixed and blocking results in
their separate checkouts when comparing them.

The `blocking` and `fixed` labels only select output filenames. They do not
switch the server implementation. The older version runs synchronous inference
inside `async def predict`; the current version uses `def predict`, which
FastAPI runs in its thread pool.

Compare runs with the same machine, model, prompt, token limit, worker count,
and warm-up procedure. Record hardware and software versions with new results.
A thread pool can improve responsiveness without making CPU inference faster.
Single runs are descriptive. This benchmark does not probe `/health` during
inference, so it cannot by itself measure health-check responsiveness.
