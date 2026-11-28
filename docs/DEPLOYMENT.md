# Deployment

How the two Jetstream2 instances are set up right now, and a log of the changes made to them. The README has the general steps; this file says what is actually running. It names files and settings, never their values. It has no IP addresses, because this repository is public: ask Pavlos for them.

Owner: Pavlos (GPU host, Compose, ports and secrets). Update this file in the same PR as a change to either instance, or in a follow-up PR straight after a change made by hand.

## CPU instance (`team-3`)

The stack testers use.

| | |
| --- | --- |
| Checkout | `~/app-assignment-1`, on `main` at `bb39928`. The running images were built from it on 2026-11-27 |
| Compose project | `app-assignment-1`, as in the README. Use `docker compose -p app-assignment-1 ...` |
| Services | `gateway`, `postgres`, `proxy`, `prometheus`, `grafana` (at `/grafana` through the proxy), and `grafana-db-setup`, which runs once at each start and exits. Only the proxy publishes a port, 8080 |
| Settings | `env/.env`: `APP_ENVIRONMENT=production`, `APP_DATABASE_URL`, `APP_JEV_MODE`, `APP_JEV_API_KEY`, `APP_PROVIDER_MODE`, `APP_VLLM_SMALL_BASE_URL` and `APP_VLLM_LARGE_BASE_URL`. `env/postgres.env`: `POSTGRES_PASSWORD`. `env/grafana.env`: `GRAFANA_DB_PASSWORD`, for the read-only `grafana_ro` role. All three are mode 600 and ignored by Git. `env/compose.defaults.env` still supplies the non-secret defaults |
| Modes | `/v1/health` reports Jev **real** (TypeSafe, one paid call per chat message) and Qwen **mock**. Jev's route answer picks the model; its intent answer goes unused, because the semantic monitor compares only like with like. Testers stay on real Jev (Sebastian, #136) |
| Not set yet | `APP_OPERATOR_TOKEN`, so the operator routes answer 403. `APP_VLLM_API_KEY` is not in `env/.env`, as the README requires: `compose.real.yml` passes it in from `~/.vllm_api_key` |
| Start at boot | `smart-gateway-cpu.service` (`deploy/autostart/`), installed with `PROJECT_NAME=app-assignment-1`. The version on `main`, installed 2026-11-27: `up --wait --wait-timeout 300 --no-build --pull never`, 3 tries. It starts mock Qwen; see the next changes |
| Backups | `~/backups/` (mode 700): a `pg_dump -Fc` of the gateway database, taken before each update, mode 600 |
| Other stacks | `guacamole` is Jetstream2's web desktop. Leave it alone |

Separate clones for branch work, so the deployed checkout stays on `main`:
- `~/eval-runner`: the evaluation runner
- `~/gpu-smoke`: vLLM smoke tests, a single-branch clone

The vLLM API key is in `~/.vllm_api_key`, mode 600.

**Public address.** Testers reach the app at the address in `USER_BRIEF.md`, the only file that holds it. It is the instance's public IP, and it changes if someone shelves the instance and lets the IP be released. To keep it, keep the public IP when shelving in Exosphere. If it changes anyway, update `USER_BRIEF.md` straight after the unshelve, and tell the testers.

## GPU instance (`gateway-gpu`)

The two vLLM servers. Only the CPU instance can reach them, over the private network.

| | |
| --- | --- |
| Checkout | `~/app-assignment-1`, on `feat/autostart`. #113 has merged, so it can move to `main` |
| Compose project | `smart-gateway-gpu` (`compose.gpu.yml`) |
| Servers | `vllm-small` (Qwen3-1.7B) on 8001 and `vllm-large` (Qwen3-4B) on 8002, bound to the private IP only |
| Settings | `env/gpu.env`: `VLLM_BIND_IP`, `MODEL_CACHE_DIR`, `VLLM_API_KEY`, and `HF_HUB_OFFLINE=1`, so starts never contact Hugging Face. Ignored by Git |
| Model weights | The attached volume at `/media/volume/gpu-models` (`MODEL_CACHE_DIR`) |
| Start at boot | `smart-gateway-gpu.service`, the version from `b9a93b7`. It checks the bind IP, both pinned models in full and the GPU, then starts `vllm-small`, then `vllm-large`, with no build or pull. About 3 minutes |

## Change log

Newest first. Times are UTC.

### 2026-11-27

- **CPU: updated from `claude/project-thread-7wkd4i` at `53c366e` to `main` at `bb39928`.** It adds Grafana (#129), migrations 005 and 006, and the start-at-boot unit from the #113 review.
  - **Before:** a `pg_dump` of the database went to `~/backups/`. `env/grafana.env` was made with a new random `GRAFANA_DB_PASSWORD`; `docker compose config` showed that `grafana` and `grafana-db-setup` get it, not the public default.
  - **The update:** `docker compose -p app-assignment-1 up -d --build --wait --wait-timeout 300` exited 0. It pulled `grafana/grafana:12.3.0`, so the boot unit's `--pull never` finds it. `grafana-db-setup` exited 0, and `--wait` did not treat that as a failure.
  - **Checks:**
    - `model_releases` and `monitor_samples` exist, and the database held 16 requests and 0 interventions afterwards.
    - The old vLLM key is gone from the gateway container's environment, now that the stack has been recreated.
    - `/grafana/api/health` answers 200. The README's statement-timeout check was run for the first time on a live stack: a 10 s query sent as an anonymous viewer through `/grafana/api/ds/query` was cancelled with "canceling statement due to statement timeout".
    - From outside, `/v1/health` and `/grafana/` answer 200, and `/metrics` gives the React app, not metrics.
  - **Smoke test:** `scripts/smoke_compose.py` fails on this stack, as expected: it requires mock Jev (`router == "mock_jev"`), and testers have real Jev. Everything it checks before that passed, as did the first chat's intent, model and format check. That chat was one paid TypeSafe call.
- **CPU: start-at-boot unit reinstalled** from `main` with the README's steps. This host's Compose has `--no-build`, `--pull never` and `--wait-timeout`. After `systemctl restart`, the unit reached `active (exited)`, every service was healthy again, and `/v1/health` reported Jev real and Qwen mock.

### 2026-11-26

- **CPU: found off `main`, with new settings.** Before a planned update to `main`, the deployed checkout was on `claude/project-thread-7wkd4i` (checked out 2026-11-25 17:21), and the stack had been rebuilt from it at 20:14. `env/.env` had been changed at 17:43 the same day: it now sets `APP_JEV_MODE` and `APP_JEV_API_KEY` (Jev is real for testers), `APP_PROVIDER_MODE` (Qwen mock), both `APP_VLLM_*_BASE_URL`, and `APP_VLLM_API_KEY`. Not recorded here at the time.
- **CPU: vLLM key taken out of `env/.env`.** The key belongs only in `env/gpu.env` on the GPU host and `~/.vllm_api_key` here. The line was removed from `env/.env` and from the private backup copy made just before. The gateway runs mock Qwen, so nothing changed for testers. The running container keeps the old value in its environment until the stack is recreated.
- **GPU: shelved** after the second real test-set run (#109).

### 2026-11-25

- **GPU: shelved, and could not be unshelved for a while.** Jetstream2 had no GPU capacity. Once it came back, the unit started both servers at boot with nobody logged in.
- **GPU: new start-at-boot unit installed** from `feat/autostart` at `b9a93b7` (#113 review fixes). Before installing:
  - `gpu-preflight.sh`, run by hand, printed `ready` against the real cache, so both pinned snapshots are complete, `tokenizer.json` included.
  - This host's Compose supports `--no-build`, `--pull never` and `--wait-timeout`.

  After `systemctl restart`, the unit reached `active (exited)`: the preflight passed as root, then `down`, then `up` waited until both servers were healthy.

### 2026-11-24

- **CPU: public IP changed.** It was released during an earlier shelve, so `USER_BRIEF.md` pointed at an address that no longer answered. The brief now has the current one, which serves the app and `/docs` from outside.

- **CPU: off the public settings.**
  - **Before:** the stack had run on `env/compose.defaults.env` alone since it was deployed, with the public throwaway database password and `APP_ENVIRONMENT` left at `local`. The database port was never published, so only containers on the Compose network could use that password.
  - **The change:** the password was changed with `ALTER USER` inside Postgres, then written to new `env/postgres.env` and `env/.env` files, with `APP_ENVIRONMENT=production`. The stack was recreated.
  - **Result:** the gateway now refuses to start with the public database password or the demo operator token. The stored history was kept, because the volume was never deleted. The smoke test passes.
- **CPU: updated from `78a52d9` to `main` at `1ed9c0b`** (#96 and #106). The only change to the running gateway is in `config.py`: optional vLLM settings, unset, and the demo-token refusal. The smoke test passes.
- **Both: start at boot (#113).** The units were installed on both instances, then the instances were shelved and unshelved. Both stacks came back in order with nobody logged in, and the GPU private IP did not change.
- **GPU: `HF_HUB_OFFLINE=1`** was confirmed in `env/gpu.env` and in both running containers.

### 2026-11-23

- **GPU: instance created** (GRID A100X-20C), and both vLLM servers started with an API key, bound to the private IP (#96). The model weights went onto an attached 50 GB volume. The API key was copied to the CPU instance's `~/.vllm_api_key`.

## Next changes, and who makes them

- **Each update to the CPU stack:** back up the database to `~/backups/` first. If the update adds an image the stack did not have, `up --build` pulls it; the boot unit never pulls.
- **A smoke test for this stack:** `scripts/smoke_compose.py` checks the all-mock stack only, so it cannot pass with real Jev. It needs a real-Jev mode before it can check a deployment.
- **GPU: move the checkout to `main`.** The unit there is already the new version; only `git checkout main && git pull` is left.
- **Real models on the deployment:** main can call vLLM (#117). Both `APP_VLLM_*_BASE_URL` are already in `env/.env`. The key is never written there: start the stack with `compose.real.yml`, which sets `APP_PROVIDER_MODE=real` and passes the key in from `~/.vllm_api_key` (README, "Real Qwen in Compose"), and change the boot unit to do the same, or a reboot brings back mock Qwen. Pavlos does this, because he holds the key, once the team agrees testers should get the real models. #137 updates `USER_BRIEF.md` for it and should merge at the same time. The rerun on #109 had format failures of 0.8% (Qwen3-1.7B) and 0.03% (Qwen3-4B), against the controller's 0.5 threshold. The GPU host must then be up whenever the CPU stack is, or every chat answers 503.
- **The operator token:** if operators need the routes on the deployment, set a long random `APP_OPERATOR_TOKEN` in `env/.env`. Never use the demo token; the gateway refuses it in production.
