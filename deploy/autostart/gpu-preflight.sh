#!/bin/sh
# Checks the GPU host is ready before smart-gateway-gpu.service starts vLLM.
# Run from the checkout. Things that come up a little after boot (the private
# IP, the volume, the GPU driver) get up to PREFLIGHT_WAIT seconds, 300 by
# default; then it exits non-zero with the reason, and systemd tries again a
# few times (see the unit's StartLimitBurst).
# Prints no values from env/gpu.env except the IP and the cache path.
set -eu

env_file=env/gpu.env
wait_for=${PREFLIGHT_WAIT:-300}
step=${PREFLIGHT_STEP:-10}

if [ ! -f "$env_file" ]; then
    echo "$env_file is missing: copy env/gpu.env.example and fill it in" >&2
    exit 1
fi

# One setting from env/gpu.env, read the way Compose reads it: the last
# `KEY=value` line wins, with CRLF endings, a leading `export`, spaces around
# the value, matching single or double quotes, and a ` #` comment after an
# unquoted value all allowed.
setting() {
    tr -d '\r' <"$env_file" | sed -n \
        -e "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=[[:space:]]*//p" \
        | tail -n 1 | sed \
        -e "s/^\"\(.*\)\"[[:space:]]*\(#.*\)\{0,1\}$/\1/;t" \
        -e "s/^'\(.*\)'[[:space:]]*\(#.*\)\{0,1\}$/\1/;t" \
        -e 's/[[:space:]]\{1,\}#.*$//' -e 's/[[:space:]]*$//'
}
ip=$(setting VLLM_BIND_IP)
cache=$(setting MODEL_CACHE_DIR)

case $ip in
    "") echo "VLLM_BIND_IP is not set in $env_file" >&2; exit 1 ;;
esac
if ! printf '%s\n' "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
    echo "VLLM_BIND_IP=$ip is not an IPv4 address; fix $env_file" >&2
    exit 1
fi
case $cache in
    "") echo "MODEL_CACHE_DIR is not set in $env_file" >&2; exit 1 ;;
esac

# The model and revision pairs compose.gpu.yml serves, one per line.
pairs=$(awk '
    /- --model=/ { sub(/.*--model=/, ""); model = $0 }
    /- --revision=/ { sub(/.*--revision=/, ""); print model, $0 }
' compose.gpu.yml)
if [ -z "$pairs" ]; then
    echo "compose.gpu.yml names no --model and --revision" >&2
    exit 1
fi

# Each check prints why it failed and returns non-zero.

# The private IP the ports bind to must belong to this host. An unshelve can
# hand out a new one; then update VLLM_BIND_IP and the CPU side's URLs.
check_ip() {
    if ! ip -4 -o addr show | grep -qF "inet $ip/"; then
        echo "VLLM_BIND_IP=$ip is not an address of this host; update $env_file"
        return 1
    fi
}

# The cache must be the attached volume, holding both pinned snapshots in
# full. With HF_HUB_OFFLINE=1, a missing file makes vLLM fail only after the
# slow health check; without it, vLLM would download about 12 GB onto the
# root disk, which has no room for it.
check_weights() {
    if [ ! -d "$cache/hub" ]; then
        echo "MODEL_CACHE_DIR=$cache has no hub/ folder: is the volume mounted?"
        return 1
    fi
    printf '%s\n' "$pairs" | while read -r model revision; do
        repo="$cache/hub/models--$(printf '%s' "$model" | sed 's|/|--|g')"
        snapshot="$repo/snapshots/$revision"
        # Snapshot files link into blobs/; -s follows the link, so a link to
        # a missing or empty blob fails too.
        for name in config.json tokenizer.json; do
            if [ ! -s "$snapshot/$name" ]; then
                echo "$model at $revision has no $name in $cache: download it again"
                return 1
            fi
        done
        if [ -f "$snapshot/model.safetensors.index.json" ]; then
            shards=$(grep -o '"[^"]*\.safetensors"' \
                "$snapshot/model.safetensors.index.json" | tr -d '"' | sort -u)
        else
            shards=model.safetensors
        fi
        for shard in $shards; do
            if [ ! -s "$snapshot/$shard" ]; then
                echo "$model at $revision is missing $shard in $cache: download it again"
                return 1
            fi
        done
        if [ -n "$(find "$repo/blobs" -name '*.incomplete' 2>/dev/null)" ]; then
            echo "$model has an unfinished download in $cache: download it again"
            return 1
        fi
    done
}

# The GPU and its driver must be up.
check_gpu() {
    if ! nvidia-smi -L >/dev/null 2>&1; then
        echo "nvidia-smi cannot see the GPU"
        return 1
    fi
}

waited=0
while :; do
    if reason=$(check_ip && check_weights && check_gpu); then
        exit 0
    fi
    if [ "$waited" -ge "$wait_for" ]; then
        echo "$reason (waited ${waited}s)" >&2
        exit 1
    fi
    sleep "$step"
    waited=$((waited + step))
done
