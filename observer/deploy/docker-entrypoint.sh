#!/bin/sh
# First start: create the data directory (/data) with the release configuration.
# Every start: make sure the read-only InfluxDB token exists, then serve.
# (The data collector has its own image: Dockerfile.collector.)
set -e
observer init --profile release >/dev/null
if [ "${1:-serve}" = "serve" ]; then
    observer influx-setup || echo "influx-setup failed: the agent tools and exports need the read-only token" >&2
    exec observer serve --host 0.0.0.0 --port 8090
fi
exec observer "$@"
