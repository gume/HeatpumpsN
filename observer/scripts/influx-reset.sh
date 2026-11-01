#!/usr/bin/env bash
# Destroy the observer's InfluxDB and build it again from scratch.
# DESTRUCTIVE: every recorded sample is lost. Needs --yes-delete-all-data.
# Org, bucket and admin token come from docker-compose.dev.yml (init mode on
# an empty volume), so nothing else needs to be restored. The observer keeps
# running and resumes writing once the database is back.
set -euo pipefail
cd "$(dirname "$0")/.."
# The database holds recorded house data: refuse unless asked explicitly.
if [ "${1:-}" != "--yes-delete-all-data" ]; then
  echo "This deletes ALL recorded observer data (volume observer-influx-dev)." >&2
  echo "Run again with --yes-delete-all-data if that is really intended." >&2
  exit 2
fi
docker compose -f docker-compose.dev.yml down -v   # -v removes the data volume
docker compose -f docker-compose.dev.yml up -d
for i in $(seq 1 60); do
  if curl -fsS http://localhost:38086/health >/dev/null 2>&1; then
    echo "InfluxDB ready on http://localhost:38086 (org heatpumpsn, bucket observer)"
    exit 0
  fi
  sleep 1
done
echo "InfluxDB did not become healthy within 60 s" >&2
exit 1
