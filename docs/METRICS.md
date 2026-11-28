# Metric definitions

Generated from [`config/metrics.yaml`](../config/metrics.yaml), the metrics catalog. Edit the YAML and run `uv run python scripts/build_metrics_docs.py`; a test fails if this page is stale. The catalog holds what each metric means and the numbers built on top of the Prometheus series (which are listed in the [README](../README.md#metrics-and-prometheus)). Settings that differ per deployment stay `APP_` settings in `config.py`; an entry only names them. `tests/test_metrics_catalog.py` fails when the dashboard, the alerts or the docs stop matching it.

Target status: *proposal* was picked from medians and not measured, *starting point* is a prototype alert threshold, *goal* is a stated aim. None of these is a measured result.

## Targets

| Target | Value | Status | Meaning |
| --- | --- | --- | --- |
| `availability` | 0.99 | proposal | Share of chat requests answered without a 503. |
| `answer_latency_p95_seconds` | small 2, large 4 | proposal | p95 time to answer a request, per model, 503s left out. Chosen from the real-Qwen medians; p95 has not been measured. |
| `request_latency_p95_alert_seconds` | 10 | starting_point | p95 time to handle any request, 503s included, above which ChatLatencyHigh fires. A looser bound than the answer target, because a 503 can wait out a model timeout. |
| `model_failure_early_warning` | 0.25 | starting_point | Monitor failure rate at which ModelFailureRateRising fires. It must stay below the controller threshold, APP_MONITOR_FAILURE_THRESHOLD. |
| `model_failure_min_samples` | 5 | starting_point | Calls in the monitor window before ModelFailureRateRising fires. It repeats APP_MONITOR_MIN_SAMPLES: a test checks it equals the default, but a deployment that changes the setting must change the alert too. |
| `chat_unavailable_alert` | 0.2 | starting_point | Share of requests that get a 503 above which ChatMostlyFailing fires. |
| `jev_skipped_alert` | 0.5 | starting_point | Share of requests routed by the rule router above which JevMostlySkipped fires. |
| `detect_faulty_model_seconds` | 30 | goal | Seconds from a model's fault starting to the controller disabling it. The estimated real-Qwen hang time is 186 s (5 requests at about 31 s each, not a measured run), so a hung model would miss it. |

## Metrics

### `chat_unavailable_ratio`

503s over all chat requests (answered plus 503). Zero-filled, because the answered counter has no series until the first reply.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Target:** at most `chat_unavailable_alert`
- **Used by:** alert:ChatMostlyFailing; docs:README.md; docs:docs/MONITORING.md
- **PromQL** over 5 minutes:

  ```promql
  ((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) / (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) + ((sum(rate(gateway_chat_requests_total[5m]))) or vector(0)))
  ```

### `chat_availability`

Share of chat requests answered without a 503.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Target:** at least `availability`
- **Used by:** grafana:Answered, no 503
- **PromQL** over 5 minutes:

  ```promql
  1 - (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) / (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) + ((sum(rate(gateway_chat_requests_total[5m]))) or vector(0))))
  ```

### `error_budget_burn_rate`

How fast the 503 allowance is spent: the 503 share over what the availability target allows. 1 spends it at exactly the budgeted pace.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Used by:** grafana:Availability burn rate (target 99% answered)
- **PromQL** over 5 minutes:

  ```promql
  (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) / (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) + ((sum(rate(gateway_chat_requests_total[5m]))) or vector(0)))) / 0.01
  ```

### `error_budget_left`

1 means no 503s, 0 means the allowance is used up, below 0 means the availability target is missed. Over the window given, not 30 days.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Used by:** grafana:Error budget left
- **PromQL** over 5 minutes:

  ```promql
  1 - ((((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) / (((sum(rate(gateway_chat_unavailable_total[5m]))) or vector(0)) + ((sum(rate(gateway_chat_requests_total[5m]))) or vector(0)))) / 0.01)
  ```

### `answer_latency_p95_seconds`

p95 time to answer a request, per model, measured inside the gateway. 503s are not in it. Not the same as request_latency_p95_seconds.

- **Unit:** seconds; **kind:** latency
- **Status:** derived in PromQL
- **Target:** at most `answer_latency_p95_seconds`
- **Used by:** grafana:p95 latency vs target (small 2 s, large 4 s); docs:README.md; docs:docs/MONITORING.md
- **PromQL** over 5 minutes (small model shown):

  ```promql
  histogram_quantile(0.95, sum by (le) (rate( gateway_chat_answer_latency_seconds_bucket{model="qwen3-1.7b"}[5m])))
  ```

### `answer_latency_within_target`

Share of a model's answered requests faster than its p95 target. Exact only because the target is a histogram bucket edge (a test checks).

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Target:** at least `answer_latency_p95_seconds`
- **Used by:** grafana:Share of requests under the latency target
- **PromQL** over 5 minutes (small model shown):

  ```promql
  sum(rate(gateway_chat_answer_latency_seconds_bucket{model="qwen3-1.7b",le="2.0"}[5m])) / sum(rate(gateway_chat_answer_latency_seconds_count{model="qwen3-1.7b"}[5m]))
  ```

### `request_latency_p95_seconds`

p95 time to handle any chat request, 503s included, not split by model. This is the alert's latency. It is not the answer latency target's.

- **Unit:** seconds; **kind:** latency
- **Status:** derived in PromQL
- **Target:** at most `request_latency_p95_alert_seconds`
- **Used by:** alert:ChatLatencyHigh; grafana:Request latency percentiles, all requests incl. 503s
- **PromQL** over 5 minutes:

  ```promql
  histogram_quantile(0.95, sum by (le) (rate( gateway_chat_latency_seconds_bucket[5m])))
  ```

### `monitor_failure_rate`

Share of a model's last calls (the monitor window) that were format failures or failed calls. This is the controller's input. It is not the share of replies that fail the format check; see format_check_pass_ratio.

- **Unit:** ratio; **kind:** gauge
- **Status:** served by the gateway
- **Target:** at most `model_failure_early_warning`
- **Related settings:** `APP_MONITOR_FAILURE_THRESHOLD`, `APP_MONITOR_WINDOW`, `APP_MONITOR_MIN_SAMPLES`
- **Used by:** alert:ModelFailureRateRising; grafana:Monitor failure rate and window size; docs:docs/MONITORING.md
- **PromQL** over 5 minutes:

  ```promql
  gateway_monitor_failure_rate
  ```

### `format_check_pass_ratio`

Share of answered requests, per model, whose reply passed the format check. 1 minus this is the format-failure share of requests.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Used by:** grafana:Format check pass rate by model
- **PromQL** over 5 minutes:

  ```promql
  sum by (model) (rate(gateway_chat_requests_total{format_check="passed"}[5m])) / sum by (model) (rate(gateway_chat_requests_total[5m]))
  ```

### `semantic_agreement_ratio`

Share of scored replies, per model, whose intent equals Jev's intent answer. Replies without a usable Jev answer are not scored.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Used by:** grafana:Qwen vs Jev intent agreement by model
- **PromQL** over 5 minutes:

  ```promql
  sum by (model) (rate(gateway_semantic_samples_total{sample="agree"}[5m])) / sum by (model) (rate(gateway_semantic_samples_total{sample=~"agree|disagree"}[5m]))
  ```

### `semantic_disagreement_rate`

The semantic monitor's window, as the controller sees it: the share of scored replies whose intent differs from Jev's. Absent until a reply is scored. The threshold is the baseline plus the margin, both settings.

- **Unit:** ratio; **kind:** gauge
- **Status:** served by the gateway
- **Related settings:** `APP_SEMANTIC_BASELINE_DISAGREEMENT_RATE`, `APP_SEMANTIC_ALERT_MARGIN`, `APP_SEMANTIC_WINDOW`, `APP_SEMANTIC_MIN_SAMPLES`
- **Used by:** grafana:Semantic window: disagreement and coverage
- **PromQL** over 5 minutes:

  ```promql
  gateway_semantic_disagreement_rate
  ```

### `jev_skipped_ratio`

Share of requests routed by the rule router because the Jev call failed, its route answer was invalid, or the circuit breaker was open.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **Target:** at most `jev_skipped_alert`
- **Used by:** alert:JevMostlySkipped
- **PromQL** over 5 minutes:

  ```promql
  sum(rate(gateway_jev_fallbacks_total[5m])) / (((sum(rate(gateway_chat_requests_total[5m]))) or vector(0)) + sum(rate(gateway_chat_unavailable_total[5m])))
  ```

### `escalation_rate`

Share of answered requests the router sent down the complex route. This is the router's decision (Jev, or the rule router when Jev is skipped), not which model answered: a complex request answered by the small model during a fallback still counts. See large_model_share for the other view.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **PromQL** over 5 minutes:

  ```promql
  sum(rate(gateway_chat_requests_total{route="complex"}[5m])) / sum(rate(gateway_chat_requests_total[5m]))
  ```

### `large_model_share`

Share of answered requests the large model answered, whatever the route. It includes standard requests moved to the large model when the small one is disabled or fails, and leaves out complex requests the small model answered. During self-healing it differs from escalation_rate.

- **Unit:** ratio; **kind:** ratio
- **Status:** derived in PromQL
- **PromQL** over 5 minutes:

  ```promql
  sum(rate(gateway_chat_requests_total{model="qwen3-4b"}[5m])) / sum(rate(gateway_chat_requests_total[5m]))
  ```

### `jev_estimated_cost_usd`

Estimated Jev cost over the window: input tokens times APP_JEV_INPUT_PRICE_PER_MILLION_USD. No data while the price is unset, so an unknown price never reads as $0. Mock Jev reports 0 tokens.

- **Unit:** usd; **kind:** cost
- **Status:** derived in PromQL
- **Related settings:** `APP_JEV_INPUT_PRICE_PER_MILLION_USD`
- **Used by:** grafana:Jev est. cost (range)
- **PromQL** over 5 minutes:

  ```promql
  sum(increase(gateway_jev_estimated_cost_usd_total[5m]))
  ```

### `jev_cost_per_request_usd`

Estimated Jev cost per chat request (answered plus 503; Jev is called before either). Jev only: the Qwen models run on our own GPUs and no per-request Qwen cost is measured.

- **Unit:** usd; **kind:** cost
- **Status:** derived in PromQL
- **Related settings:** `APP_JEV_INPUT_PRICE_PER_MILLION_USD`
- **PromQL** over 5 minutes:

  ```promql
  (sum(increase(gateway_jev_estimated_cost_usd_total[5m]))) / (((sum(increase(gateway_chat_unavailable_total[5m]))) or vector(0)) + ((sum(increase(gateway_chat_requests_total[5m]))) or vector(0)))
  ```

### `time_to_disable_seconds`

Seconds from a fault starting to the controller disabling the model. Measured per fault by a script; there is no live metric, and no panel can show it. The Health screen and the interventions table show when a model was disabled, not when its fault began. The seconds depend on the traffic rate: the same fault takes longer when fewer requests reach the model. `requests_to_detect` in the same script's output is the steadier measure.

- **Unit:** seconds; **kind:** duration
- **Status:** offline: measured by a script, not scraped
- **Target:** at most `detect_faulty_model_seconds`
- **Used by:** script:scripts/measure_recovery.py
- **Measured by:** `scripts/measure_recovery.py#detect_seconds`
