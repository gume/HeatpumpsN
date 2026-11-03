#!/bin/sh
# First start: create the data directory (/data) with the release configuration.
# Every start: make sure the read-only InfluxDB token exists, then serve.
# "collect" (the collector container) only stores data: on the very first start it
# waits for the portal to create the data directory, so the two never write it at once.
set -e
if [ "${1:-serve}" = "collect" ]; then
    i=0
    while [ ! -f "$OBSERVER_HOME/config.yaml" ] && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
    observer init --profile release >/dev/null
    exec observer collect
fi
observer init --profile release >/dev/null
if [ "${1:-serve}" = "serve" ]; then
    observer influx-setup || echo "influx-setup failed: the agent tools and exports need the read-only token" >&2
    exec observer serve --host 0.0.0.0 --port 8090
fi
exec observer "$@"
