#!/usr/bin/env bash
# Back up the gateway database with pg_dump, check the file reads back, copy it
# off the instance and prune old local copies.
#
#   deploy/backup.sh [--label NAME]
#
# Writes $BACKUP_DIR/gateway-<UTC time>[-NAME].dump (pg_dump custom format,
# mode 600) and prints its path as the last line. With BACKUP_REMOTE set
# (user@host:directory) it copies the file there with scp. Without it, it says
# the copy was skipped and still succeeds, because a local backup before a
# deploy is worth having on its own.
# Needs the postgres container running. Nothing in the dump is message text:
# the gateway never stores it.
set -euo pipefail
# shellcheck source=deploy/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

label=""
while [ $# -gt 0 ]; do
    case $1 in
        --label)
            [ $# -ge 2 ] || die "--label needs a name"
            label=$2
            shift 2
            ;;
        *) die "usage: deploy/backup.sh [--label NAME]" ;;
    esac
done
case $label in
    *[!A-Za-z0-9_-]*) die "the label may hold only letters, digits, - and _" ;;
esac

postgres_running || die "the postgres container is not running; nothing to back up"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$BACKUP_DIR/gateway-$stamp${label:+-$label}.dump"
partial="$file.partial"
trap 'rm -f "$partial"' EXIT

say "Dumping the database to $file"
# The container's own user and database names; no password is needed on the
# container's local socket, so none passes through here.
compose exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$partial"
[ -s "$partial" ] || die "pg_dump wrote an empty file"

# Reading the table of contents back proves the file is a whole archive.
entries=$(compose exec -T postgres pg_restore --list <"$partial" | grep -c . || true)
[ "$entries" -gt 0 ] || die "the dump does not read back with pg_restore"
mv "$partial" "$file"
chmod 600 "$file"
say "Checked: pg_restore reads $entries entries"

if [ -n "$BACKUP_REMOTE" ]; then
    say "Copying to $BACKUP_REMOTE"
    # BACKUP_SCP_OPTS is split on purpose, so it can hold several options.
    # shellcheck disable=SC2086
    scp -q -o BatchMode=yes $BACKUP_SCP_OPTS "$file" "$BACKUP_REMOTE/" ||
        die "the copy to $BACKUP_REMOTE failed; the local backup is at $file"
    copied=yes
else
    say "WARNING: BACKUP_REMOTE is not set, so this backup is only on this instance"
    copied=no
fi

# Keep the newest BACKUP_KEEP files.
# shellcheck disable=SC2012
ls -1t "$BACKUP_DIR"/gateway-*.dump 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" |
    while read -r old; do rm -f -- "$old"; done

log_line "action=backup file=$(basename "$file") entries=$entries copied_off_instance=$copied result=ok"
say "$file"
