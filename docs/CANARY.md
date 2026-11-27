# Canary releases

The canary splitter sends a share of requests to a new config version, so the team can compare it with the current one before switching everyone over. It is built in `src/app_assignment_1/release.py`. Nothing here has been measured on real models: the backends are still mocks.

## Versions

A version is a named set of gateway config, listed in `config/versions.json` (`APP_VERSIONS_PATH`). Today a version only chooses the mock backend config. Once the Qwen backends are real, it should also carry the prompt.

| Version | What it is |
|---|---|
| `v1` | The mock backends from `APP_BACKENDS_PATH`, so the chaos and outage Compose overrides still apply to it. |
| `v2-chaos` | A demo of a bad release: `config/backends.chaos.json`, where the small model corrupts every reply. |

## The release

The release names the stable version, an optional canary version and the canary's share of requests, from 0 to 100%. At startup the gateway uses the release from settings (`APP_STABLE_VERSION`, default `v1`; `APP_CANARY_VERSION`; `APP_CANARY_PERCENT`). After a change is saved, the store's latest release wins, so it survives a restart. A saved release that names a version no longer in `versions.json` is ignored with a warning. Once you change releases with `PUT`, clear `APP_CANARY_VERSION` and `APP_CANARY_PERCENT` from your env, so a restart that cannot read the store does not bring back an old canary.

`GET /v1/release` says where the current release came from in `source`: `store` (the latest saved change), `settings` (nothing saved yet), or `settings_store_unreadable`. The last means the store could not be read, so a saved release, such as a rollback, may be waiting there. The gateway tries again on the next `GET /v1/release`, `PUT` or `/v1/health`. Docker checks `/v1/health` every 10 seconds, so this usually clears within seconds.

Each request gets a bucket from 0 to 99, taken from a hash of its request ID. Requests with a bucket below the canary's percent go to the canary. So raising the percent only moves stable requests to the canary, and the choice can be worked out again from a saved request. There is no login or session, so one customer's requests can land on different versions.

Every reply's trace records `version_id` and `canary`. `/v1/health` shows them for recent requests, and `/metrics` counts requests by version, role (stable or canary), the model that answered and format check (`gateway_version_requests_total`). Compare one model at a time: if a model is disabled, the other one answers its route, so a canary that breaks one model can look healthy in the totals.

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

`/v1` is public through Caddy, so a change needs the operator token (`APP_OPERATOR_TOKEN`, at least 16 characters), sent as `Authorization: Bearer <token>`. Without a token set, changes are turned off and `PUT` returns 403. The token is checked before the body, so a caller without it gets 401 or 403, never the validation details. `GET /v1/release` needs no token, so anyone can read the `reason` of recent changes: keep it free of anything private.

A change is saved before it is applied. If the save fails, the gateway reads the latest saved release back, because a save that timed out may still have been written:

- It was written: the change is applied and `PUT` returns 200.
- It was not: `PUT` returns 503, "The release could not be saved", and nothing changes.
- The store cannot be read either: `PUT` returns 503, "may or may not have been saved". Check `GET /v1/release` before trying again.

If the request is cancelled during the save, such as by a client disconnect, the gateway still finishes the save and applies the change, so it and a restarted gateway agree. An automatic rollback waits for any operator change in progress, then rolls back only if the canary it judged is still the current one.

## Limits

- **The controller never sees canary requests.** The monitor and controller count failures per model, so canary calls are left out of them: a bad canary cannot disable the model stable traffic uses, and cannot trigger a model rollback from `releases.json`. A canary reply also adds no semantic sample and does not count toward confirming a model release. The usage on `/v1/health` counts stable traffic only. The cost is that the controller's windows describe stable traffic only. Comparing a canary with stable inside the monitor would need it to group by `version_id` (the "Canary comparison" in the monitor records draft, #45).
- **The canary rolls itself back.** The gateway keeps a separate window of canary calls (failed calls and failed format checks), using the monitor's window size, minimum samples and failure threshold. When it shows the canary failing, the gateway saves a release with no canary, so only the canary goes and stable keeps serving. The history shows the reason, "Automatic rollback: canary ... kept failing". This only catches failed calls and failed format checks, not wrong intents.
- **Mock backends only.** With `APP_PROVIDER_MODE=real` the gateway runs the vLLM backends as its only version, so there is nothing to split between.
- **Rolling back the gateway image.** Every request saved from this version on has `version_id` and `canary` in its trace. A gateway image from before the canary splitter rejects those fields, so after rolling back to one, `/v1/requests/{id}` and the recent requests on `/v1/health` fail for those rows. From this version on, rows are read leniently: fields a newer gateway added are ignored.
- **One gateway process.** Each process holds the release in memory and loads it only at startup, so a change made through one process does not reach another.
- **No screen yet.** The Deployments screen (Verness) can read `GET /v1/release`.
