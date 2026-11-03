#!/usr/bin/env bash
# Install or update the observer in one install folder.
#
#   ~/heatpumpsn-observer/app/scripts/update.sh heatpumpsn-observer-0.6.0.tar.gz   # an update
#   heatpumpsn-observer-0.6.0/scripts/update.sh                                    # first install,
#        [--dir ~/heatpumpsn-observer] [--from ~/heatpumpsn-observer-0.5.0]        # from an unpacked bundle
#
# The install folder separates what is yours from the code:
#   .env, data/, docker-compose.override.yml   yours: never replaced, never deleted
#   app/                                        the release: replaced by every update
#   docker-compose.yml, docker-compose.sandbox.yml   links into app/deploy/
# InfluxDB keeps the samples in its named volume (<project>_influx-data).
#
# Steps: build the new images first (a failing build changes nothing), keep
# the previous code as .app-previous (a roll back; cleanup.sh removes it),
# switch app/, then `docker compose up -d`: containers whose image and
# settings did not change keep running - an unchanged collector is not
# restarted. Coming from the layout before 0.6.0 (a folder per version, data in
# the volume <project>_observer-data), --from takes the old .env and the data
# volume is copied into data/ once; the volume itself stays until cleanup.sh
# --old-volume.
#
# Options: --dir DIR (install folder; default: the one this script is in, or
# ~/heatpumpsn-observer), --from OLD (the old bundle folder, for its .env),
# --no-start (prepare only).
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)            # the bundle or app/ this script belongs to
project=${COMPOSE_PROJECT_NAME:-heatpumpsn-observer}
root="" from="" source="" start=1
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) root=$2; shift 2 ;;
        --from) from=$2; shift 2 ;;
        --no-start) start=0; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) source=$1; shift ;;
    esac
done
if [ -z "$root" ]; then
    if [ "$(basename "$here")" = app ] && [ -e "$here/../.env" -o -d "$here/../data" ]; then
        root=$(dirname "$here")
    else
        root="$HOME/heatpumpsn-observer"
    fi
fi
mkdir -p "$root"
root=$(cd "$root" && pwd)
cd "$root"
say() { printf '%s\n' "$*"; }

# --- the new code ---------------------------------------------------------------------------
rm -rf .update
mkdir .update
if [ -n "$source" ]; then
    [ -f "$source" ] || { say "no such bundle: $source"; exit 1; }
    tar xzf "$source" -C .update
    new=$(find .update -mindepth 1 -maxdepth 1 -type d | head -1)
else
    [ "$here" != "$root/app" ] || { say "give the new bundle: $0 heatpumpsn-observer-<version>.tar.gz"; exit 1; }
    cp -a "$here" .update/bundle
    new=.update/bundle
fi
for f in Dockerfile Dockerfile.collector observer deploy/docker-compose.yml; do
    [ -e "$new/$f" ] || { say "not an observer bundle (no $f): ${source:-$here}"; rm -rf .update; exit 1; }
done
tag() { sed -n "s/^ *image: \\($1:[^ ]*\\).*/\\1/p" "$new/deploy/docker-compose.yml" | head -1; }
obs_image=$(tag heatpumpsn-observer)
col_image=$(tag heatpumpsn-collector)
current=$( [ -f app/pyproject.toml ] && sed -n 's/^version = "\(.*\)"/\1/p' app/pyproject.toml || echo none)
say "install folder: $root"
say "observer: $current -> ${obs_image#*:}   collector image: ${col_image#*:}"

# --- yours: .env and data/ ------------------------------------------------------------------
if [ ! -f .env ]; then
    old_env=""
    for c in "$from/deploy/.env" "$from/.env"; do [ -n "$from" ] && [ -f "$c" ] && old_env=$c && break; done
    if [ -n "$old_env" ]; then
        cp "$old_env" .env && say ".env taken from $old_env"
    else
        cp "$new/deploy/env.example" .env
        say ".env created from the example: fill it in (passwords, tokens, broker), then: cd $root && docker compose up -d"
        start=0
    fi
    chmod 600 .env
fi
envval() { sed -n "s/^$1=//p" .env | tail -1 | tr -d '"'"'"; }
uid=$(envval OBSERVER_UID); gid=$(envval OBSERVER_GID)
uid=${uid:-1000}; gid=${gid:-1000}
if [ "$(id -u)" != "$uid" ]; then
    say "note: the containers run as uid $uid, you are $(id -u): data/ is written as $uid."
    say "      To own data/ yourself, add OBSERVER_UID=$(id -u) and OBSERVER_GID=$(id -g) to .env."
fi
mkdir -p data

# --- build the new images before touching anything that runs ---------------------------------
if [ "$start" = 1 ]; then
    say "building $obs_image"
    docker build -q -t "$obs_image" -f "$new/Dockerfile" "$new" >/dev/null
    # an existing collector image is kept only when it was built from exactly these files
    digest=$(sed -n 's/^ *COLLECTOR_DIGEST: "\([^"]*\)".*/\1/p' "$new/deploy/docker-compose.yml" | head -1)
    have=$(docker image inspect -f '{{index .Config.Labels "org.heatpumpsn.collector.digest"}}' "$col_image" 2>/dev/null || true)
    if [ -n "$digest" ] && [ "$have" = "$digest" ]; then
        say "$col_image is current: the collector is not rebuilt"
    else
        say "building $col_image"
        docker build -q -t "$col_image" --build-arg "COLLECTOR_DIGEST=$digest" -f "$new/Dockerfile.collector" "$new" >/dev/null
    fi
fi

# --- once: the data volume of the layout before 0.6.0 into data/ -------------------------------
old_volume="${project}_observer-data"
if [ ! -f data/config.yaml ] && docker volume inspect "$old_volume" >/dev/null 2>&1; then
    say "moving the observer's data from the volume $old_volume into $root/data (the volume is kept)"
    docker stop "${project}-observer-1" "${project}-collector-1" >/dev/null 2>&1 || true
    docker run --rm --user 0 --entrypoint sh -v "$old_volume:/from:ro" -v "$root/data:/to" "$obs_image" \
        -c "cp -a /from/. /to/ && chown -R $uid:$gid /to"
    [ -f data/config.yaml ] || { say "the copy did not bring config.yaml: stopping here"; exit 1; }
fi
if [ "$start" = 1 ] && [ "$(stat -c %u data)" != "$uid" ]; then       # the containers write data/ as uid
    docker run --rm --user 0 --entrypoint chown -v "$root/data:/d" "$obs_image" -R "$uid:$gid" /d
fi

# --- switch the code ------------------------------------------------------------------------
rm -rf .app-previous
[ -d app ] && mv app .app-previous
mv "$new" app
rm -rf .update
ln -sfn app/deploy/docker-compose.yml docker-compose.yml
ln -sfn app/deploy/docker-compose.sandbox.yml docker-compose.sandbox.yml
cf=$(envval COMPOSE_FILE)
if [ -n "$cf" ]; then
    IFS=: read -ra parts <<< "$cf"
    for f in "${parts[@]}"; do [ -e "$f" ] || say "warning: COMPOSE_FILE in .env names $f, which does not exist here"; done
fi

if [ "$start" = 0 ]; then
    say "prepared; start with: cd $root && docker compose up -d"
    exit 0
fi
before=$(docker compose ps -q collector 2>/dev/null || true)
docker compose up -d
after=$(docker compose ps -q collector 2>/dev/null || true)
if [ -n "$before" ] && [ "$before" = "$after" ]; then
    say "collector: unchanged, kept running"
else
    say "collector: (re)started"
fi
docker compose ps
[ -d .app-previous ] && say "previous code kept in $root/.app-previous (roll back: mv app .app-broken && mv .app-previous app && docker compose up -d)"
exit 0
