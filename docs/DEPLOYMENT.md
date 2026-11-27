# Deployment

How the two Jetstream2 instances are set up right now, and a log of the changes made to them. The README has the general steps; this file says what is actually running. It names files and settings, never their values. It has no IP addresses, because this repository is public: ask Pavlos for them.

Owner: Pavlos (GPU host, Compose, ports and secrets). Update this file in the same PR as a change to either instance, or in a follow-up PR straight after a change made by hand.

## CPU instance (`team-3`)

The stack testers use.

| | |
| --- | --- |
| Checkout | `~/app-assignment-1`, on `claude/project-thread-7wkd4i` (the #110 branch, since merged) at `53c366e`, not `main`. The running images were built from it on 2026-11-25. See the change log |
| Compose project | `app-assignment-1`, as in the README. Use `docker compose -p app-assignment-1 ...` |
| Services | `gateway`, `postgres`, `proxy`, `prometheus`. Only the proxy publishes a port, 8080 |
| Settings | `env/.env`: `APP_ENVIRONMENT=production`, `APP_DATABASE_URL`, `APP_JEV_MODE`, `APP_JEV_API_KEY`, `APP_PROVIDER_MODE`, `APP_VLLM_SMALL_BASE_URL` and `APP_VLLM_LARGE_BASE_URL`. `env/postgres.env`: `POSTGRES_PASSWORD`. Both are mode 600 and ignored by Git. `env/compose.defaults.env` still supplies the non-secret defaults |
| Modes | `/v1/health` reports Jev **real** (TypeSafe, one paid call per chat message) and Qwen **mock**. Jev's route answer picks the model; its intent answer goes unused, because the semantic monitor compares only like with like |
| Not set yet | `APP_OPERATOR_TOKEN`, so the operator routes answer 403. `APP_VLLM_API_KEY` is not in `env/.env`, as the README requires: `compose.real.yml` passes it in from `~/.vllm_api_key` |
| Start at boot | `smart-gateway-cpu.service` (`deploy/autostart/`), installed with `PROJECT_NAME=app-assignment-1`. Still the 2026-11-24 version from before the #113 review; the reinstall is below |
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

- **Pavlos: update the CPU checkout to `main` and rebuild**, once the person who switched it to `claude/project-thread-7wkd4i` confirms they are done with it. Back up the database first: `main` adds migrations 004 and 006.
- **The team: should testers keep real Jev?** Each chat message is a paid TypeSafe call. Its route answer picks the model, but while Qwen is mock the semantic monitor ignores its intent answer. The update to `main` keeps `env/.env` as it is, so Jev stays real unless someone changes `APP_JEV_MODE`.
- **Pavlos, now that #113 has merged: reinstall the CPU unit**, with the README's install steps. The new unit adds `--no-build --pull never`, a wait limit and a 3-try limit. First check that the CPU host's Compose has those flags (`docker compose up --help`). The GPU unit is already the new version; there, only `git checkout main && git pull` is left.
- **Real models on the deployment:** main can call vLLM (#117). Both `APP_VLLM_*_BASE_URL` are already in `env/.env`. The key is never written there: start the stack with `compose.real.yml`, which sets `APP_PROVIDER_MODE=real` and passes the key in from `~/.vllm_api_key` (README, "Real Qwen in Compose"), and change the boot unit to do the same, or a reboot brings back mock Qwen. Pavlos does this, because he holds the key, once the team agrees testers should get the real models. The rerun on #109 had format failures of 0.8% (Qwen3-1.7B) and 0.03% (Qwen3-4B), against the controller's 0.5 threshold. The GPU host must then be up whenever the CPU stack is, or every chat answers 503.
- **The operator token:** if operators need the routes on the deployment, set a long random `APP_OPERATOR_TOKEN` in `env/.env`. Never use the demo token; the gateway refuses it in production.
