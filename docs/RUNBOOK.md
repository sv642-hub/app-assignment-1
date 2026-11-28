# Runbook

Routine tasks for the Smart AI Gateway on the Jetstream2 CPU instance: a fresh install, a deploy, a rollback, a backup, a restore and a reboot. Each task has the exact commands and what you should see when it worked.

Read this with [DEPLOYMENT.md](DEPLOYMENT.md) (what runs where right now, and a change log) and [MONITORING.md](MONITORING.md) (what to do when an alert fires).

## Status of this runbook

Parts of the deploy, backup, restore and HTTPS work are being built in other pull requests. Where a step depends on one, it is marked **PENDING** with the file it waits for. Until that file merges, use the manual commands shown beside it, which work on `main` today. Nothing marked **NOT YET RUN** has been run on the instance; the result goes in the table at the end of that task.

| Marker | Meaning |
| --- | --- |
| **PENDING** | The script or file does not exist on `main` yet. Replace the manual steps with it when it merges |
| **NOT YET RUN** | The commands are written but nobody has run them on the instance, so the "you should see" text is what we expect |
| Unmarked | Run on the instance, or taken from a check already recorded in DEPLOYMENT.md |

## Before you start

You need:

- SSH access to the CPU instance as `exouser`. Ask Pavlos for the address; this repository is public and has no IPs. If you do not have a key, use the Jetstream2 web shell instead (Exosphere, instance page, Web shell). In the web shell `read` prompts do not work, so use `nano` to enter any secret.
- Docker with Compose 2.24 or newer on the instance. Check with `docker compose version`.
- The files `env/.env`, `env/postgres.env` and `env/grafana.env` on the instance. They are mode 600 and ignored by Git, so a fresh clone does not have them. [Fresh install](#1-fresh-install) creates them.

Every command below runs on the instance, in `~/app-assignment-1`, unless it says otherwise. The Compose project is `app-assignment-1`, so commands use `docker compose -p app-assignment-1`.

Never paste a key, a password or a token into a pull request, an issue or a chat. Enter them with `nano`.

---

## 1. Fresh install

Use this on a new instance, or after the checkout was deleted.

**1. Get the code.**

```bash
git clone https://github.com/sv642-hub/app-assignment-1.git ~/app-assignment-1
cd ~/app-assignment-1
git checkout main
git log --oneline -1
```

You should see the latest commit on `main`.

**2. Create the private settings files.** The password goes into two files so the database and the gateway agree. Do this before the first start: Postgres reads the password only when its volume is first created.

```bash
PW=$(openssl rand -hex 24)
printf 'POSTGRES_PASSWORD=%s\n' "$PW" > env/postgres.env
printf 'APP_ENVIRONMENT=production\nAPP_DATABASE_URL=postgresql://gateway:%s@postgres:5432/gateway\n' "$PW" > env/.env
printf 'GRAFANA_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" > env/grafana.env
chmod 600 env/.env env/postgres.env env/grafana.env
unset PW
ls -l env/.env env/postgres.env env/grafana.env
```

You should see three files with `-rw-------`. Never use the public throwaway values in `env/compose.defaults.env`; the gateway refuses to start with them when `APP_ENVIRONMENT=production`.

Optional settings, added to `env/.env` with `nano env/.env`:

| Setting | When |
| --- | --- |
| `APP_JEV_MODE=real` and `APP_JEV_API_KEY` | Real Jev. Each chat message is one paid TypeSafe call |
| `APP_OPERATOR_TOKEN` | Operator routes. Use a long random value, never the demo token |
| `APP_PROVIDER_MODE=real`, `APP_VLLM_SMALL_BASE_URL`, `APP_VLLM_LARGE_BASE_URL` | Real Qwen. The key is not written here; see [Real Qwen](#real-qwen-optional) |

The README's [Configuration and env files](../README.md#configuration-and-env-files) section lists every setting.

**3. Build and start.**

```bash
docker compose -p app-assignment-1 up -d --build --wait --wait-timeout 300
echo "exit code: $?"
```

You should see `exit code: 0`. The first build takes a few minutes. `grafana-db-setup` runs once and exits; that is expected and `--wait` does not treat it as a failure.

**4. Check it.**

```bash
docker compose -p app-assignment-1 ps
curl -s http://localhost:8080/v1/health | head -c 200; echo
python3 scripts/smoke_compose.py http://localhost:8080
```

You should see:

- `gateway`, `postgres`, `proxy`, `prometheus` and `grafana` as `running (healthy)`; `grafana-db-setup` as `exited (0)`.
- The health response starting with JSON that contains `"status":"ok"`.
- With mock Jev: the smoke test ending in `PASS: frontend, docs, health, chat and history through http://localhost:8080`. With real Jev the smoke test fails by design (it requires mock Jev) and each chat is a paid call; check `/v1/health` by hand instead.

**5. Install start at boot** (once per instance). See [Start at boot](../README.md#start-at-boot) for what the unit does.

```bash
PROJECT=app-assignment-1
sed -e "s|CHECKOUT_DIR|$PWD|g" -e "s|PROJECT_NAME|$PROJECT|g" \
  deploy/autostart/smart-gateway-cpu.service \
  | sudo tee /etc/systemd/system/smart-gateway-cpu.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now smart-gateway-cpu
systemctl status smart-gateway-cpu --no-pager | head -5
```

You should see `Active: active (exited)`.

**6. HTTPS.** **PENDING:** the HTTPS Caddyfile (ports 80 and 443, certificate for the instance's name). Today the app is served as plain HTTP on port 8080. When it merges, replace this step with the commands from that pull request and add: `curl -sI https://<instance-name>/v1/health` should print `HTTP/2 200`, and `curl -sI http://<instance-name>/` should redirect to HTTPS.

### Real Qwen (optional)

The two vLLM servers live on the GPU instance and are reachable only over the private network. The key is never in `env/.env`:

```bash
export VLLM_API_KEY=$(cat ~/.vllm_api_key)
APP_VLLM_API_KEY="$VLLM_API_KEY" docker compose -p app-assignment-1 -f compose.yml -f compose.real.yml up -d --wait
curl -s http://localhost:8080/v1/health | grep -o '"mode":"[a-z]*"'
```

You should see `"mode":"real"`. If every chat answers 503, the GPU instance is shelved or its servers are not up; see [Reboot](#6-reboot).

---

## 2. Deploy

**PENDING: `scripts/deploy.sh`.** The deploy script pulls a released image from the GitHub Container Registry, records its digest, starts it, checks it and writes a line to the deploy log. When it merges, a deploy is:

```bash
# PENDING: scripts/deploy.sh <version>      e.g. scripts/deploy.sh 0.3.0
# Expected: a line in the deploy log with the version, the image digest and "ok".
```

Until then, deploy from `main` by hand. Back up first, because a deploy may run a new database migration.

**1. Back up the database** (see [Backup](#4-backup)).

**2. Note what is running now**, so you can go back to it:

```bash
git log --oneline -1
docker compose -p app-assignment-1 images
```

Write the commit hash in the change log in [DEPLOYMENT.md](DEPLOYMENT.md).

**3. Update and rebuild.**

```bash
git fetch origin
git checkout main
git pull --ff-only
docker compose -p app-assignment-1 up -d --build --wait --wait-timeout 300
echo "exit code: $?"
```

You should see `exit code: 0`. Migrations run once at gateway start, each in one transaction, under a lock. The gateway log shows them:

```bash
docker compose -p app-assignment-1 logs gateway --tail=50 | grep -i migrat
```

**4. Check.**

```bash
curl -s http://localhost:8080/v1/health | grep -o '"status":"[a-z]*"'
docker compose -p app-assignment-1 ps
```

You should see `"status":"ok"` and every service healthy. Chat once from the browser and confirm a reply. If the check fails, [roll back](#3-rollback).

**5. Record it** in the change log in DEPLOYMENT.md: the commit, the time (UTC) and the checks you ran.

---

## 3. Rollback

**PENDING: `scripts/deploy.sh`.** Rolling back is the same script with an older version:

```bash
# PENDING: scripts/deploy.sh <previous-version>      e.g. scripts/deploy.sh 0.2.0
```

Until then, roll back to the commit you noted in step 2 of the deploy.

```bash
git checkout <previous-commit>
docker compose -p app-assignment-1 up -d --build --wait --wait-timeout 300
curl -s http://localhost:8080/v1/health | grep -o '"status":"[a-z]*"'
```

You should see `"status":"ok"`.

**Database changes.** Migrations are one-way: nothing undoes a migration. Old code runs against a newer schema only when the change was additive (a new table or a nullable column). A migration that removes or renames something must be split into steps (expand, then contract in a later release) so the version you might roll back to still works. If a rollback fails on the schema, restore the backup you took before the deploy; see [Restore](#5-restore).

**Not the same thing: a model release rollback.** The gateway also rolls a *model's* release back by itself when a new release fails while watched (see [README, Releases and rollback](../README.md#releases-and-rollback)). That changes which version a model serves, not which gateway code runs. An operator can do it too:

```bash
curl -s -X POST -H "Authorization: Bearer $APP_OPERATOR_TOKEN" \
  http://localhost:8080/v1/operator/models/qwen3-1.7b/rollback
```

You should see the model's release returned as JSON. Without `APP_OPERATOR_TOKEN` set on the server this answers 403.

---

## 4. Backup

The database holds the request history, controller interventions, release events and monitor samples. It does not hold customer messages; those are never stored. The Jev key, the vLLM key and the passwords are in the `env/` files, which are not in the database; see [What a backup does not cover](#what-a-backup-does-not-cover).

**PENDING: `scripts/backup.sh`.** When it merges, a backup is one command and prints the file it wrote. Until then:

```bash
mkdir -p ~/backups && chmod 700 ~/backups
FILE=~/backups/gateway-$(date -u +%Y%m%dT%H%M%SZ).dump
docker compose -p app-assignment-1 exec -T postgres pg_dump -U gateway -Fc gateway > "$FILE"
chmod 600 "$FILE"
ls -l "$FILE"
```

You should see a file with mode `-rw-------` and a size above zero (a few hundred KB or more once there are requests). Check that it is a readable dump:

```bash
docker compose -p app-assignment-1 exec -T postgres pg_restore -l < "$FILE" | head -5
```

You should see a list of table entries such as `TABLE public requests`. An error or an empty list means the dump is bad; take another.

Back up before every deploy and before any migration. Copy a recent dump off the instance, because the instance and its volume are one failure:

```bash
# From your laptop:
scp exouser@<instance-address>:backups/gateway-<timestamp>.dump .
```

### What a backup does not cover

| Not covered | Where it is kept instead |
| --- | --- |
| `env/.env`, `env/postgres.env`, `env/grafana.env` | Only on the instance (mode 600). Keep a private copy somewhere safe: a restore onto a new instance needs the same `POSTGRES_PASSWORD` or a new matching `APP_DATABASE_URL` |
| `~/.vllm_api_key`, the Jev key | Same: private copy. Never in Git |
| Prometheus data and Grafana state | Named volumes; not backed up. Metrics restart from empty, and the dashboards are provisioned from the repository |

---

## 5. Restore

Restore a dump into a stopped-gateway database, then start the gateway. **NOT YET RUN.** A restore has not been tested on the instance yet; the result goes in the table below.

**PENDING: `scripts/restore.sh <dump-file>`.** Until then:

**1. Stop the gateway** so nothing writes during the restore. Postgres stays up.

```bash
docker compose -p app-assignment-1 stop gateway proxy
```

**2. Restore.** `--clean --if-exists` drops the objects in the dump before recreating them, so the database ends up as the dump was.

```bash
FILE=~/backups/gateway-<timestamp>.dump
docker compose -p app-assignment-1 exec -T postgres \
  pg_restore -U gateway -d gateway --clean --if-exists --no-owner < "$FILE"
echo "exit code: $?"
```

You should see `exit code: 0`. Warnings about objects that do not exist are fine with `--if-exists`.

**3. Start everything and check.**

```bash
docker compose -p app-assignment-1 up -d --wait --wait-timeout 300
docker compose -p app-assignment-1 exec -T postgres \
  psql -U gateway -c 'SELECT count(*) AS requests FROM requests'
curl -s http://localhost:8080/v1/health | grep -o '"status":"[a-z]*"'
```

You should see the same request count the database had when the dump was taken, and `"status":"ok"`. The gateway rebuilds the monitor windows and running cooldowns from the restored `monitor_samples` and interventions at startup.

**A restore onto a new instance:** do [Fresh install](#1-fresh-install) steps 1 and 2 first (the password in the new `env/` files becomes the new database's password), start only `postgres` with `docker compose -p app-assignment-1 up -d --wait postgres`, then run step 2 above.

| Date | Dump taken | Restored into | Row count before / after | Result | Who |
| --- | --- | --- | --- | --- | --- |
| _to be filled after the first restore on the instance_ | | | | | |

---

## 6. Reboot

**NOT YET RUN on the final stack.** The start-at-boot unit was tested on 2026-11-24 and 2026-11-27 (see DEPLOYMENT.md); it has not been run with the HTTPS proxy, backups and real Qwen. Fill in the table after Sebastian reboots the instance.

A reboot brings the stack back by itself: `smart-gateway-cpu.service` runs `docker compose down` and then `up -d --wait --no-build --pull never`, up to 3 tries. It never builds or pulls, so it starts the images from the last deploy. Named volumes keep the database across the reboot.

**1. Reboot.**

```bash
sudo reboot
```

Your SSH session ends. Wait about two minutes, then log in again.

**2. Check the unit and the stack.**

```bash
systemctl status smart-gateway-cpu --no-pager | head -5
docker compose -p app-assignment-1 ps
curl -s http://localhost:8080/v1/health | grep -o '"status":"[a-z]*"'
```

You should see `Active: active (exited)`, every service healthy and `"status":"ok"`.

**3. What a reboot resets.** The gateway restarts with every model in service. The monitor windows and a running cooldown are rebuilt from Postgres. The Jev circuit breaker and the call totals start from zero (the breaker is not saved).

**4. If the unit failed.**

```bash
sudo journalctl -u smart-gateway-cpu -b --no-pager | tail -30
sudo systemctl reset-failed smart-gateway-cpu
sudo systemctl restart smart-gateway-cpu
```

The log names the reason. A common one is a missing image: the unit never builds, so run the deploy step once by hand.

**5. The GPU instance.** If real Qwen is on, the GPU instance must be up too, or every chat answers 503. It has its own unit, `smart-gateway-gpu.service`, which checks the private IP, the model volume and the GPU, then starts `vllm-small` and `vllm-large` in order (about 3 minutes). On the GPU instance:

```bash
systemctl status smart-gateway-gpu --no-pager | head -5
curl -s -H "Authorization: Bearer $(cat ~/.vllm_api_key)" http://<gpu-private-ip>:8001/v1/models | head -c 120; echo
```

(Run the `curl` from the CPU instance, which holds `~/.vllm_api_key`.) You should see JSON listing the model.

**6. Shelving.** Shelve both instances when idle to save allocation. Keep the public IP when shelving in Exosphere; if it changes, update `USER_BRIEF.md` and tell the testers.

| Date | What was rebooted | Time until healthy | Result | Who |
| --- | --- | --- | --- | --- |
| _to be filled after the reboot on the final stack_ | | | | |

---

## When something is wrong

| Symptom | First check |
| --- | --- |
| The page does not load | `docker compose -p app-assignment-1 ps`: is `proxy` healthy? Then `docker compose -p app-assignment-1 logs proxy --tail=50` |
| Every chat answers 503 | `curl -s http://localhost:8080/v1/health`: are both models disabled? With real Qwen, is the GPU instance up? See [MONITORING.md](MONITORING.md) |
| `/v1/health` says `"status":"degraded"` | A model is disabled by the controller. It re-enables itself after the cooldown (60 s by default); see the interventions in the health response |
| Gateway restarts in a loop | `docker compose -p app-assignment-1 logs gateway --tail=50`. A bad setting fails at startup with the setting's name, never its value |
| An alert fired | The alert's section in [MONITORING.md](MONITORING.md) |
| Disk is full | `docker system df`; remove unused images with `docker image prune`. Never `docker compose down -v`: it deletes the database volume |
