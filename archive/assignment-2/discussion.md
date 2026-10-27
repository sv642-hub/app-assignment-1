# Problem 2: Blocking-Call Audit

## Setup

The service runs SmolLM2-135M-Instruct locally using PyTorch and Transformers on CPU, on an Apple M4 MacBook Pro with 16 GB of memory. The model and tokenizer are loaded once per application process at startup and reused for requests.

Both versions used one Uvicorn worker, the same model, `max_new_tokens=64`, and the same input:

> What is a bank? Answer in one short sentence.

Each benchmark performed one warm-up request, excluded from timing, followed by ten concurrent requests using `httpx.AsyncClient` and `asyncio.gather`. No artificial sleep was added.

## Processing Stages for One Request

The following measurements come from one warmed-up, in-process request-processing sequence using `scripts/measure_steps.py`. They exclude model loading, HTTP transport, thread-pool scheduling, and queueing.

| Step | Computing or waiting? | Work performed | Approximate time |
|---|---|---|---:|
| Input validation | Computing | Pydantic validates the request text. | 0.051 ms |
| Model generation | Computing overall; the calling thread waits synchronously for inference to finish | Tokenize the prompt, execute local CPU inference, and decode the generated tokens. | 288.621 ms |
| Response construction and serialization | Computing | Construct `PredictResponse` and serialize it to JSON. | 0.083 ms |

Model generation dominates these measured processing stages. In the blocking version, this synchronous work runs directly inside the async endpoint and prevents the event loop from handling other work during the call. In the fixed version, FastAPI runs the synchronous endpoint in its thread pool.

## Ten-Request Results

| Version | Endpoint implementation | Total wall-clock time | Slowest request |
|---|---|---:|---:|
| Blocking | `async def predict` directly calls synchronous `generator.generate()` | 3.249 s | 3.248 s |
| Fixed | Plain `def predict` calls the same synchronous generator | 4.038 s | 4.037 s |

In the blocking run, individual request times increased from 0.306 seconds to 3.248 seconds, consistent with requests waiting while earlier model calls occupied the event loop. In the fixed run, request completion times clustered between 3.935 and 4.037 seconds.

The fixed run took about 24% longer overall. Moving inference to the thread pool allows concurrent execution but does not reduce the CPU work required; CPU contention and scheduling overhead are plausible contributors to the increase, although this experiment does not isolate their individual effects.

These are single benchmark runs, so the results are descriptive rather than a statistical performance comparison. They do not directly measure health-check responsiveness during inference.

## Reproduction and Saved Evidence

Start the installed version of the service with one worker:

```bash
uv run --no-sync uvicorn app_assignment_1.api:app --workers 1
```

After startup completes, run the corresponding benchmark in another terminal:

```bash
uv run --no-sync python scripts/benchmark_requests.py blocking
```

or:

```bash
uv run --no-sync python scripts/benchmark_requests.py fixed
```

The label controls the output filename; it does not switch the endpoint implementation. Stop the server and install the appropriate code version before each run. For the local non-editable installation used in this experiment:

```bash
uv sync --extra dev --frozen --no-editable --reinstall-package app-assignment-1
```

Saved evidence:

- Blocking implementation: Git tag `problem2-blocking-v1`, commit `f76721c`.
- Blocking measurements: `benchmark_results/blocking.json`.
- Fixed measurements: `benchmark_results/fixed.json`.
- Concurrent-request benchmark: `scripts/benchmark_requests.py`.
- Single-request stage measurements: `scripts/measure_steps.py`.

At the time of these measurements, the model settings were explicit values in the startup code. They now come from the shared Pydantic settings (`APP_LLM_MODEL_NAME`, `APP_LLM_MAX_NEW_TOKENS`), with the same defaults used here.