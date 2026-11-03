#!/usr/bin/env bash
# What is surely not needed any more in an observer installation: listed, and
# deleted only with --yes.
#
#   ~/heatpumpsn-observer/app/scripts/cleanup.sh            # list
#   ~/heatpumpsn-observer/app/scripts/cleanup.sh --yes      # delete
#   ... --old-volume                                        # also the pre-0.6.0 data volume (see below)
#
# Candidates:
#   .app-previous, .update          the code before the last update (the roll back), leftovers
#   *.tar.gz bundles                observer bundles in the install folder and next to it
#   old bundle folders              heatpumpsn-observer-<version>/ next to the install folder (the
#                                   layout before 0.6.0) - kept when their .env differs from yours
#   observer and collector images   versions no container uses, and their dangling build layers
#   --old-volume                    <project>_observer-data, the data volume before 0.6.0: only
#                                   after it was copied to data/ (data/config.yaml exists)
# Never: .env, data/, docker-compose.override.yml, the InfluxDB volumes (the recorded samples).
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
project=${COMPOSE_PROJECT_NAME:-heatpumpsn-observer}
root="" yes=0 old_volume=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) root=$2; shift 2 ;;
        --yes) yes=1; shift ;;
        --old-volume) old_volume=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown option $1"; exit 1 ;;
    esac
done
root=${root:-$(dirname "$here")}
root=$(cd "$root" && pwd)
[ -f "$root/.env" ] && [ -d "$root/app" ] || { echo "$root is not an install folder (.env and app/)"; exit 1; }
cd "$root"
found=0
item() { found=1; printf '  %-60s %s\n' "$1" "$2"; }
size() { du -sh "$1" 2>/dev/null | cut -f1; }

echo "install folder: $root"
echo "surely not needed:"
files=()
for d in .app-previous .update; do
    [ -e "$d" ] && item "$root/$d" "$(size "$d")" && files+=("$root/$d")
done
for f in "$root"/heatpumpsn-observer-*.tar.gz "$(dirname "$root")"/heatpumpsn-observer-*.tar.gz; do
    [ -f "$f" ] && item "$f" "$(size "$f")" && files+=("$f")
done
for d in "$(dirname "$root")"/heatpumpsn-observer-*/; do
    d=${d%/}
    [ -d "$d" ] && [ "$d" != "$root" ] && [ -f "$d/Dockerfile" ] && [ -d "$d/observer" ] || continue
    keep=""
    for e in "$d/deploy/.env" "$d/.env"; do
        [ -f "$e" ] && ! cmp -s "$e" "$root/.env" && keep="its .env differs from $root/.env"
    done
    if [ -n "$keep" ]; then
        echo "  (kept: $d - $keep)"
    else
        item "$d" "$(size "$d")"
        files+=("$d")
    fi
done

images=()
used=$(docker ps -a --format '{{.Image}}' | sort -u)
while read -r ref; do
    [ -n "$ref" ] || continue
    grep -qxF "$ref" <<< "$used" && continue
    item "image $ref" "$(docker image inspect -f '{{.Size}}' "$ref" | awk '{printf "%.0f MB", $1/1e6}')"
    images+=("$ref")
done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^heatpumpsn-(observer|collector):' || true)
dangling=$(docker image ls -q --filter dangling=true --filter label=org.heatpumpsn.component | wc -l)
[ "$dangling" -gt 0 ] && item "dangling observer/collector build layers" "$dangling images"

volume=""
if [ "$old_volume" = 1 ]; then
    v="${project}_observer-data"
    if docker volume inspect "$v" >/dev/null 2>&1; then
        if [ -f data/config.yaml ] && ! docker ps -a --filter volume="$v" -q | grep -q .; then
            item "volume $v (the data before 0.6.0, copied to data/)" ""
            volume=$v
        else
            echo "  (kept: volume $v - not copied to data/ yet, or a container still uses it)"
        fi
    fi
fi

if [ "$found" = 0 ]; then
    echo "  nothing"
    exit 0
fi
if [ "$yes" = 0 ]; then
    echo "delete with: $0 --yes$( [ "$old_volume" = 1 ] && echo " --old-volume" || true)"
    exit 0
fi
[ ${#files[@]} -gt 0 ] && rm -rf "${files[@]}"
[ ${#images[@]} -gt 0 ] && docker image rm "${images[@]}" >/dev/null
[ "$dangling" -gt 0 ] && docker image prune -f --filter label=org.heatpumpsn.component >/dev/null
[ -n "$volume" ] && docker volume rm "$volume" >/dev/null
echo "deleted."
