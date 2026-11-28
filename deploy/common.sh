# Shared settings and helpers for deploy.sh, backup.sh and restore.sh. Source
# it; do not run it. It moves to the repository root, reads env/deploy.env
# when there is one (template: env/deploy.env.example), and sets the Compose
# command every script uses.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# Settings given on the command line win over env/deploy.env.
_before=$(export -p)
if [ -f env/deploy.env ]; then
    set -a
    # shellcheck disable=SC1091
    . env/deploy.env
    set +a
    eval "$_before"
fi
unset _before

# A leading ~ in a setting is the home directory (the shell does not expand it
# inside a sourced file).
expand_home() { printf '%s' "${1/#\~/$HOME}"; }

COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-app-assignment-1}
IMAGE_REPO=${IMAGE_REPO:-ghcr.io/sv642-hub/app-assignment-1}
DEPLOY_LOG=$(expand_home "${DEPLOY_LOG:-~/deploy.log}")
STATE_DIR=$(expand_home "${STATE_DIR:-~/.smart-gateway}")
BACKUP_DIR=$(expand_home "${BACKUP_DIR:-~/backups}")
BACKUP_KEEP=${BACKUP_KEEP:-14}
BACKUP_REMOTE=${BACKUP_REMOTE:-}
BACKUP_SCP_OPTS=${BACKUP_SCP_OPTS:-}
export COMPOSE_PROJECT_NAME IMAGE_REPO

umask 077
mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 700 "$STATE_DIR" "$BACKUP_DIR"

# The address the stack answers on, for health checks run on this host.
if [ -n "${SITE_ADDRESS:-}" ]; then
    BASE_URL=${BASE_URL:-https://$SITE_ADDRESS}
else
    BASE_URL=${BASE_URL:-http://127.0.0.1:8080}
fi

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# One line to the deploy log: a timestamp, then key=value pairs.
log_line() {
    printf '%s %s\n' "$(now)" "$*" >>"$DEPLOY_LOG"
}

say() { printf '%s\n' "$*"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

# The version the stack runs now ("" before the first deploy) and the one
# before it. deploy.sh writes them after a deploy that passed its checks.
current_version() { cat "$STATE_DIR/current" 2>/dev/null || true; }
previous_version() { cat "$STATE_DIR/previous" 2>/dev/null || true; }

# docker compose with the project's files: HTTPS when SITE_ADDRESS is set, the
# published images when a version has been deployed. IMAGE_TAG picks the
# version; it defaults to the current one.
compose() {
    local files=(-f compose.yml)
    [ -n "${SITE_ADDRESS:-}" ] && files+=(-f compose.https.yml)
    IMAGE_TAG=${IMAGE_TAG:-$(current_version)}
    if [ -n "$IMAGE_TAG" ]; then
        files+=(-f compose.release.yml)
        export IMAGE_TAG
    fi
    docker compose "${files[@]}" "$@"
}

# True when the postgres container exists and is running.
postgres_running() {
    local id
    id=$(compose ps -q postgres 2>/dev/null || true)
    [ -n "$id" ] && [ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" = true ]
}

# Wait until GET /v1/ready answers 200, for up to $1 seconds.
wait_ready() {
    local limit=${1:-120} waited=0
    until curl -fsS --max-time 5 "$BASE_URL/v1/ready" >/dev/null 2>&1; do
        waited=$((waited + 3))
        [ "$waited" -ge "$limit" ] && return 1
        sleep 3
    done
}
