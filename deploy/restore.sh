#!/usr/bin/env bash
# Restore the gateway database from a backup made by deploy/backup.sh.
#
#   deploy/restore.sh --verify FILE    prove the backup restores, touching nothing live
#   deploy/restore.sh FILE --yes       replace the live database with the backup
#
# --verify restores into a scratch database, counts the rows of every table,
# and drops the scratch database. Run it after a backup to know the backup
# works before you need it.
#
# A live restore stops the gateway, takes a safety backup of what is there now
# (label pre-restore), replaces the tables inside one transaction (so a bad file
# leaves the old data in place), starts the stack again and waits for it to be
# ready. It refuses to run without --yes: it overwrites request history.
set -euo pipefail
# shellcheck source=deploy/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

verify=no
yes=no
file=""
for arg in "$@"; do
    case $arg in
        --verify) verify=yes ;;
        --yes) yes=yes ;;
        -*) die "usage: deploy/restore.sh --verify FILE | deploy/restore.sh FILE --yes" ;;
        *) file=$arg ;;
    esac
done
[ -n "$file" ] || die "usage: deploy/restore.sh --verify FILE | deploy/restore.sh FILE --yes"
[ -f "$file" ] || die "no such file: $file"
postgres_running || die "the postgres container is not running"

# Row counts of every table in a database (default: the live one), one
# "table<TAB>count" line each.
row_counts() {
    compose exec -T postgres sh -c \
        'psql -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}" -At -F "	" -v ON_ERROR_STOP=1' _ "${1:-}" <<'SQL'
SELECT table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
SQL
}

if [ "$verify" = yes ]; then
    scratch="restore_check_$(date -u +%Y%m%d%H%M%S)"
    say "Restoring $file into the scratch database $scratch"
    compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" '"$scratch"
    trap 'compose exec -T postgres sh -c "dropdb -U \"\$POSTGRES_USER\" --if-exists '"$scratch"'" >/dev/null 2>&1 || true' EXIT
    compose exec -T postgres sh -c \
        'pg_restore -U "$POSTGRES_USER" -d '"$scratch"' --no-owner --exit-on-error' <"$file"
    counts=$(row_counts "$scratch")
    say "Rows per table in the backup:"
    say "$counts" | sed 's/^/  /'
    grep -q '^schema_migrations	' <<<"$counts" || die "the backup has no schema_migrations table"
    log_line "action=restore-verify file=$(basename "$file") result=ok"
    say "OK: the backup restores. The scratch database is dropped."
    exit 0
fi

[ "$yes" = yes ] || die "this replaces the live database; run again with --yes to confirm"

say "Taking a safety backup of the live database first"
safety=$("$ROOT/deploy/backup.sh" --label pre-restore | tail -n 1)
say "Safety backup: $safety"

say "Stopping the gateway so nothing writes during the restore"
compose stop proxy gateway

# One transaction: either the whole backup is in, or the old data is untouched.
say "Restoring $file"
if ! compose exec -T postgres sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction --exit-on-error' \
    <"$file"; then
    compose start gateway proxy || true
    log_line "action=restore file=$(basename "$file") safety=$(basename "$safety") result=failed"
    die "pg_restore failed; the database is as it was. Safety backup: $safety"
fi

say "Starting the gateway"
compose up -d --wait --wait-timeout 300 --no-build --pull never
wait_ready 120 || die "restored, but $BASE_URL/v1/ready did not answer 200"
counts=$(row_counts)
say "Rows per table after the restore:"
say "$counts" | sed 's/^/  /'
log_line "action=restore file=$(basename "$file") safety=$(basename "$safety") result=ok"
say "OK: restored from $file"
