# Canary releases

The canary splitter sends a share of requests to a new config version, so the team can compare it with the current one before switching everyone over. It is built in `src/app_assignment_1/release.py`. Nothing here has been measured on real models: the backends are still mocks.

## Versions

A version is a named set of gateway config, listed in `config/versions.json` (`APP_VERSIONS_PATH`). Today a version only chooses the mock backend config. Once the Qwen backends are real, it should also carry the prompt.

| Version | What it is |
|---|---|
| `v1` | The mock backends from `APP_BACKENDS_PATH`, so the chaos and outage Compose overrides still apply to it. |
| `v2-chaos` | A demo of a bad release: `config/backends.chaos.json`, where the small model corrupts every reply. |

## The release

The release names the stable version, an optional canary version and the canary's share of requests, from 0 to 100%. At startup the gateway uses the release from settings (`APP_STABLE_VERSION`, default `v1`; `APP_CANARY_VERSION`; `APP_CANARY_PERCENT`). After a change is saved, the store's latest release wins, so it survives a restart. A saved release that names a version no longer in `versions.json` is ignored with a warning.

Each request gets a bucket from 0 to 99, taken from a hash of its request ID. Requests with a bucket below the canary's percent go to the canary. So raising the percent only moves stable requests to the canary, and the choice can be worked out again from a saved request. There is no login or session, so one customer's requests can land on different versions.

Every reply's trace records `version_id` and `canary`. `/v1/health` shows them for recent requests, and `/metrics` counts requests by version, role (stable or canary) and format check (`gateway_version_requests_total`).

## Changing the release

`GET /v1/release` shows the current release, the versions and the last 20 changes. `PUT /v1/release` changes it:

```bash
# Send 10% of requests to the bad demo release
curl -X PUT http://localhost:8080/v1/release \
  -H "Authorization: Bearer $APP_OPERATOR_TOKEN" -H "Content-Type: application/json" \
  -d '{"stable": "v1", "canary": "v2-chaos", "canary_percent": 10, "reason": "Try v2"}'

# Roll back: no canary
curl -X PUT ... -d '{"stable": "v1", "reason": "Roll back v2"}'

# Promote: the canary becomes stable
curl -X PUT ... -d '{"stable": "v2-chaos", "reason": "Promote v2"}'
```

`/v1` is public through Caddy, so a change needs the operator token (`APP_OPERATOR_TOKEN`, at least 16 characters). Without one, changes are turned off and `PUT` returns 403. A change is saved before it is applied: if the store cannot save it, `PUT` returns 503 and the release stays as it was.

## Limits

- **Per-model monitor.** The monitor and controller count failures per model, not per version. A bad canary counts against its model for everyone, and at a high enough share the controller disables that model for stable traffic too. Comparing a canary with stable needs the monitor to group by `version_id` (the "Canary comparison" in the monitor records draft, #45).
- **No automatic rollback.** Rolling back is a manual `PUT`. The controller's "Roll back" item is Sebastian's.
- **One gateway process.** Each process holds the release in memory and loads it only at startup, so a change made through one process does not reach another.
- **No screen yet.** The Deployments screen (Verness) can read `GET /v1/release`.
