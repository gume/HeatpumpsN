#!/usr/bin/env bash
# Build, flash and verify the controller in one step.
#
# Usage:
#   tests/flash_and_check.sh [runtime_check.py options]
#
# Environment:
#   HPN_ENV        PlatformIO environment to flash (default: esp32dev_sim)
#   HPN_URL        controller portal URL (default: http://192.168.2.14)
#   HPN_UPLOAD_FS  set to 1 to also upload the LittleFS image
#
# Examples:
#   tests/flash_and_check.sh
#   tests/flash_and_check.sh --serial
#   HPN_ENV=esp32s2 HPN_URL=http://192.168.2.50 tests/flash_and_check.sh
#
# The controller must be reachable through `pio remote` (a logged-in agent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="${HPN_ENV:-esp32dev_sim}"
BASE_URL="${HPN_URL:-http://192.168.2.14}"

if ! command -v pio >/dev/null 2>&1; then
  echo "error: pio (PlatformIO) is not on PATH" >&2
  exit 1
fi

cd "$ROOT/firmware"

echo "==> Flashing $ENV via pio remote"
pio remote run --force-remote -d . -e "$ENV" --target upload

if [[ "${HPN_UPLOAD_FS:-0}" == "1" ]]; then
  echo "==> Uploading LittleFS image"
  pio remote run --force-remote -d . -e "$ENV" --target uploadfs
fi

echo "==> Waiting for the controller to come back"
for _ in $(seq 1 30); do
  if python3 - "$BASE_URL" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/api/status", timeout=3) as r:
        sys.exit(0 if json.load(r).get("ok") else 1)
except Exception:
    sys.exit(1)
PY
  then
    break
  fi
  sleep 1
done

echo "==> Running runtime checks"
python3 "$ROOT/tests/runtime_check.py" --base-url "$BASE_URL" "$@"
