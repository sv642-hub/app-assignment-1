# End-to-end check

`scripts/e2e_check.py` sends seven fixed test messages through a running gateway and prints a PASS, FAIL or SKIP line for each check. It exits 1 if any check failed. It needs only Python 3.9 or newer and the standard library, so it runs on a Mac with no install.

```bash
python3 scripts/e2e_check.py --base-url http://localhost:8080
```

## What it checks

- Each reply passes the format check, contains its policy facts, and can be read back from `GET /v1/requests/{id}`.
- The serving model matches the final route in the trace. When that route differs from the one Jev picked, the trace must include a backend fallback reason. Failed calls in `/v1/health` must not rise during the run, so a down model is caught even when requests are rerouted.
- `GET /v1/health` before and after: both models answered at least one check message, completed calls and monitor samples grew, no model is disabled by the controller, no new interventions, status is `ok`. With real Jev, the Jev breaker is closed.
- With real Jev (`jev_mode` is `real`), each trace must say the router was `jev`. A reply routed by the rule router fails the check and shows its fallback reason, so a silent Jev fallback no longer hides behind a passing run.
- Prometheus counters, when you give it a metrics source: `gateway_chat_requests` and `gateway_backend_calls_completed` rose by at least the number of messages sent (more means other clients sent traffic too; a quiet stack gives the cleanest read), and the monitor gauges are exported.

## Mock run and real run

The run is labelled from the gateway: `MOCK Qwen` while `/v1/health` says `mode` is `mock`, otherwise `REAL Qwen`. Switching needs no flag.

- **Mock:** the expected intent and route of each message are asserted, because the mocks match keywords and always give the same answer.
- **Real:** the expectations come from the mock keywords, and a real model may answer differently. The script only counts how often the intent and route matched and prints that as `MEASURED`. It does not fail on disagreement and sets no pass threshold. Treat those counts as seven test messages, not an accuracy result; use the eval runner for accuracy.

Once Yanzhen's vLLM client is wired in as the backends, run it the same way. No result from real Qwen has been recorded by this script yet.

## Prometheus

`/metrics` is internal and Caddy does not proxy it, so the script cannot reach it through :8080. It picks a source in this order:

1. `--metrics-url http://<gateway-ip>:8000/metrics`, if you can reach the gateway directly.
2. `--metrics-cmd "<command>"`, a command that prints the metrics text. The script runs it once before and once after the messages.
3. By default, on a Compose host, if `docker` is installed and you run it from the repo root, the script runs `docker compose exec -T gateway python -c ...` itself, so the Prometheus checks run with no extra option. If that command fails (for example, no Compose stack in this folder), the checks are reported as SKIP with the reason, not as FAIL.

`--no-metrics` skips the Prometheus checks.

The checks compare the rise of `gateway_chat_requests` and `gateway_backend_calls_completed` with the messages sent, and the rise of completed calls with the same rise in `/v1/health`. The two must be equal, so a `--metrics-url` that points at a different gateway fails. CI runs the script this way against the mock stack in the `compose-smoke` job.

With no metrics source the Prometheus checks are reported as SKIP, not PASS.

## Privacy

The script does not print message text or save anything. The test messages are fixed examples, not customer data.
