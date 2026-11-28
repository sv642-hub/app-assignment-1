# Runbook: HTTPS, deploy, rollback, backup, restore

The scripts live in `deploy/`. Run them on the CPU instance, from the checkout. Settings go in `env/deploy.env` (template: `env/deploy.env.example`). Nothing here has been run on the instance yet; the "you should see" lines say what a good run prints.

## One-time setup

1. Point a public DNS name at the instance and open ports 80 and 443 in the Jetstream2 security group. Nothing else needs to be open to the internet except SSH.
2. `cp env/deploy.env.example env/deploy.env`, set `SITE_ADDRESS` to that name, and set `BACKUP_REMOTE` to a place that is not this instance.
3. Check the stack file: `SITE_ADDRESS=<name> IMAGE_TAG=x docker compose -f compose.yml -f compose.https.yml -f compose.release.yml config | grep -A6 ports:`. You should see only `80` and `443`.

## Deploy a release

```bash
deploy/deploy.sh v1.0.0
```

You should see: a pre-deploy backup path, the two image digests, `OK: v1.0.0 is running at https://<name>`. The same line, with the digests, is appended to `~/deploy.log`.

If the new version does not become healthy, answer 200 on `/v1/ready`, or fail the golden set (`scripts/e2e_check.py`), the script puts the previous version back and exits non-zero. `--no-golden` skips the golden set (with real Jev it sends paid calls).

## Roll back

```bash
deploy/deploy.sh previous      # or: deploy/deploy.sh v0.9.0
```

It is the same script, so the same checks run. The database is not rolled back: a migration must keep the older version working (add first, remove in a later release). To undo data too, restore the pre-deploy backup named in the log line (below).

## Back up

```bash
deploy/backup.sh
```

You should see `Checked: pg_restore reads N entries`, then the file path. The file is mode 600 in `~/backups`, copied to `BACKUP_REMOTE` with scp (a warning says so when that is unset), and the newest 14 are kept. A deploy takes one itself.

## Restore

First prove the backup works, without touching live data:

```bash
deploy/restore.sh --verify ~/backups/gateway-<time>.dump
```

You should see the row count of each table and `OK: the backup restores`. To replace the live database:

```bash
deploy/restore.sh ~/backups/gateway-<time>.dump --yes
```

It takes a safety backup (`-pre-restore`), stops the gateway, restores in one transaction, starts the stack, waits for `/v1/ready` and prints the row counts. If the restore fails, the old data is untouched.

## Check readiness

```bash
curl -fsS https://<name>/v1/ready
```

200 with `"status":"ready"` when the database answers and at least one model is reachable. 503 otherwise; the body says which one.
