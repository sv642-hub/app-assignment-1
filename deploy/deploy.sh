#!/usr/bin/env bash
# Deploy a release from GHCR, check it, and roll back if the check fails.
#
#   deploy/deploy.sh VERSION [--no-golden] [--no-rollback]
#   deploy/deploy.sh previous          roll back to the version before this one
#
# VERSION is the image tag the release workflow pushed: a release such as
# v1.2.0, or a commit sha. A rollback is this same script with an older version.
#
# What it does, in order. It stops at the first failure.
#   1. Backs up the database (deploy/backup.sh --label pre-deploy), because the
#      new version may run migrations when it starts. Skipped on a first deploy,
#      when there is no database yet.
#   2. Pulls the gateway and proxy images and records each one's digest.
#   3. Starts the stack with those images and waits for it to be healthy.
#   4. Waits for GET /v1/ready (database and a model reachable).
#   5. Runs the golden set: scripts/e2e_check.py against the public address.
#      With real Jev that sends paid calls; --no-golden skips it.
#   6. Writes one line to the deploy log and remembers the version.
# If step 3, 4 or 5 fails and an earlier version is known, it deploys that
# version again (--no-rollback turns this off). The database is not rolled back:
# migrations are expand-and-contract, so the older version still runs on the
# newer schema (docs/DEPLOYMENT.md). To undo data too, restore the
# pre-deploy backup the log line names with deploy/restore.sh.
set -euo pipefail
# shellcheck source=deploy/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

version=""
golden=yes
auto_rollback=yes
for arg in "$@"; do
    case $arg in
        --no-golden) golden=no ;;
        --no-rollback) auto_rollback=no ;;
        -*) die "usage: deploy/deploy.sh VERSION [--no-golden] [--no-rollback]" ;;
        *) version=$arg ;;
    esac
done
[ -n "$version" ] || die "usage: deploy/deploy.sh VERSION [--no-golden] [--no-rollback]"

if [ "$version" = previous ]; then
    version=$(previous_version)
    [ -n "$version" ] || die "no previous version is recorded"
fi
# Image tags: letters, digits, dot, dash and underscore, as Docker allows.
[[ $version =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "not a valid image tag: $version"

# One deploy at a time.
exec 9>"$STATE_DIR/lock"
flock -n 9 || die "another deploy is running"

# Pull one tag's images and print "gateway=<digest> proxy=<digest>".
pull_and_record() {
    local tag=$1 svc ref digest out=""
    IMAGE_TAG=$tag compose pull gateway proxy >&2 || return 1
    for svc in gateway proxy; do
        ref="$IMAGE_REPO/$svc:$tag"
        digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$ref") ||
            return 1
        out+="$svc=${digest##*@} "
    done
    printf '%s' "${out% }"
}

# Start a tag and wait until it is ready. Returns non-zero on failure.
start_and_wait() {
    local tag=$1
    IMAGE_TAG=$tag compose up -d --wait --wait-timeout "${DEPLOY_WAIT:-300}" \
        --no-build --pull never || return 1
    wait_ready "${READY_WAIT:-120}"
}

# The golden set sends 7 chats, under the rate limit's burst of 30, so the
# running gateway never answers it with a 429.
golden_check() {
    [ "$golden" = yes ] || return 0
    if [ -n "${DEPLOY_GOLDEN_CMD:-}" ]; then
        # shellcheck disable=SC2086
        $DEPLOY_GOLDEN_CMD
    else
        python3 scripts/e2e_check.py --base-url "$BASE_URL" --no-metrics
    fi
}

previous=$(current_version)
backup="none"

if [ "$version" = "$previous" ]; then
    say "Version $version is already deployed; running the checks again."
fi

# 1. Backup before anything changes.
if postgres_running; then
    say "Backing up the database before the deploy"
    backup=$("$ROOT/deploy/backup.sh" --label pre-deploy | tail -n 1)
    backup=$(basename "$backup")
else
    say "No database is running yet, so there is nothing to back up."
fi

# 2. Pull. A failed pull changes nothing.
say "Pulling $IMAGE_REPO/{gateway,proxy}:$version"
if ! digests=$(pull_and_record "$version"); then
    log_line "action=deploy version=$version previous=${previous:-none} backup=$backup result=failed reason=pull"
    die "could not pull the images for $version; nothing was changed"
fi
say "Digests: $digests"

# 3-5. Start, ready, golden set.
reason=""
if ! start_and_wait "$version"; then
    reason=start
elif ! golden_check; then
    reason=golden
fi

if [ -z "$reason" ]; then
    if [ "$version" != "$previous" ]; then
        [ -n "$previous" ] && printf '%s\n' "$previous" >"$STATE_DIR/previous"
        printf '%s\n' "$version" >"$STATE_DIR/current"
    fi
    log_line "action=deploy version=$version previous=${previous:-none} $digests backup=$backup result=ok"
    say "OK: $version is running at $BASE_URL"
    exit 0
fi

# Failed.
log_line "action=deploy version=$version previous=${previous:-none} $digests backup=$backup result=failed reason=$reason"
say "FAILED at step: $reason" >&2
if [ "$auto_rollback" = yes ] && [ -n "$previous" ] && [ "$previous" != "$version" ]; then
    say "Rolling back to $previous" >&2
    if prev_digests=$(pull_and_record "$previous") && start_and_wait "$previous"; then
        log_line "action=rollback version=$previous from=$version $prev_digests result=ok"
        die "$version failed ($reason); $previous is running again. Database backup before the deploy: $backup"
    fi
    log_line "action=rollback version=$previous from=$version result=failed"
    die "$version failed ($reason) and the rollback to $previous failed too. Look at: docker compose -p $COMPOSE_PROJECT_NAME logs"
fi
die "$version failed ($reason). Database backup before the deploy: $backup"
