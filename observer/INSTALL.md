# Installing the HeatpumpsN observer

The short path from nothing to a running observer, and how to update it.
Details and background: [`deploy/README.md`](deploy/README.md); what changed
in each version: [`CHANGELOG.md`](CHANGELOG.md).

## What it is

The observer watches a HeatpumpsN heating installation from the outside. It
records every MQTT signal of the house (the HeatpumpsN controller, EMS-ESP,
Zigbee2MQTT, Home Assistant states) in its own InfluxDB and offers a web
portal to:

* choose which signals are stored and describe what they mean (an LLM can
  propose descriptions);
* build a reviewed description of the house;
* supervise the plant and raise alerts in Home Assistant;
* run analyses with agents in a sandbox, with a library of tools and skills;
* re-identify the controller's mixer model from recorded data, backtest
  parameter sets in a simulation and, after your approval, send them signed
  to the controller.

The observer **never controls the plant**. Its outputs are reports, alerts
and parameter proposals that you approve.

It runs as three Docker containers in one install folder:

| container | job |
|---|---|
| `influxdb` | the recorded samples |
| `collector` | MQTT in, InfluxDB out; keeps running through observer updates |
| `observer` | the portal, alerts, agents, parameters, imports and exports |

## Requirements

* Linux with Docker and the compose plugin (`docker compose version`).
* The home MQTT broker reachable from this machine (host, port, user and
  password if it has one).
* Disk space: about 1.4 million samples a day, a few GB a year in InfluxDB.
* Optional: an OpenAI-compatible LLM endpoint (descriptions, house
  understanding, agents). Everything else works without one.

## 1. Get the release

A release is one file, `heatpumpsn-observer-<version>.tar.gz`, attached to
the GitHub release `observer-v<version>` of gume/HeatpumpsN:

```bash
curl -LO https://github.com/gume/HeatpumpsN/releases/download/observer-v0.6.0/heatpumpsn-observer-0.6.0.tar.gz
```

(or `gh release download observer-v0.6.0 -R gume/HeatpumpsN`, or copy it
from a development machine's `observer/dist/`).

## 2. Install from scratch

```bash
tar xzf heatpumpsn-observer-0.6.0.tar.gz
heatpumpsn-observer-0.6.0/scripts/update.sh --dir ~/heatpumpsn-observer
cd ~/heatpumpsn-observer
```

This creates the install folder with `app/` (the release), the compose
files and a `.env` from the example, builds the images and stops there,
because `.env` is new. Fill in `.env`:

| setting | value |
|---|---|
| `INFLUX_ADMIN_PASSWORD`, `INFLUX_ADMIN_TOKEN` | new secrets: `openssl rand -hex 32` each |
| `OBSERVER_MQTT_HOST`, `OBSERVER_MQTT_PORT` | the home broker |
| `OBSERVER_MQTT_USER`, `OBSERVER_MQTT_PASSWORD` | if the broker needs them |
| `OBSERVER_PORTAL_PASSWORD` | the portal's password (user `OBSERVER_PORTAL_USER`, default `admin`) |
| `TZ` | your time zone, e.g. `Europe/Budapest` |
| `OBSERVER_UID`, `OBSERVER_GID` | your `id -u` / `id -g` if not 1000, so the files in `data/` are yours |
| `OBSERVER_LLM_BASE_URL`, `OBSERVER_LLM_KEY` | optional LLM provider (a proxy on this machine: `http://host.docker.internal:<port>/v1`) |

Then start it:

```bash
docker compose up -d
docker compose ps                 # influxdb, collector and observer healthy after about a minute
```

On the first start InfluxDB creates its database, the observer creates
`data/` with the release configuration and its own read-only token, and
recording starts at once with the generic rules.

**Open the portal.** It listens on `127.0.0.1:8090` only. From another
machine use a tunnel:

```bash
ssh -L 8090:127.0.0.1:8090 <this machine>      # then http://127.0.0.1:8090/
```

or set `OBSERVER_BIND=0.0.0.0` in `.env` (only with a portal password) and
run `docker compose up -d` again. The header shows the running version.

**First steps in the portal:**

1. **Status**: the collector is connected and *Stored samples* grows.
2. **Signals**: choose what is stored; *Ask the LLM* proposes descriptions.
3. **Settings → LLM**: a model per task (type in the model field to search
   the provider's models).
4. **Agent → Library**: *Import from URL* with the library pack, e.g.
   `https://github.com/gume/observer-library/releases/download/v0.3.0/observer-library-0.3.0.tgz`.
5. **Agent → House**: write your review, generate a draft, accept it.

**Moving from another observer** (e.g. a development machine): there, Status
tab → *Export state* and a database *Export*; here, *Import state…* and
*Import a file…* (only missing samples are written, nothing is deleted).
See `deploy/README.md` §3.

**Agent sandbox** (optional; needed for agent runs and tool tests):

```bash
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.sandbox.yml' >> .env
docker compose up -d
docker compose exec observer observer sandbox-check     # must end with "sandbox OK"
```

This runs the observer container without Docker's seccomp and AppArmor
profiles so that bubblewrap can isolate the agent's code; see
`deploy/README.md` §4.

## 3. Update

The portal shows a banner when a newer release exists. Read its CHANGELOG
entry, download the bundle, then:

```bash
~/heatpumpsn-observer/app/scripts/update.sh heatpumpsn-observer-<version>.tar.gz
```

The script builds the new images first (a failing build changes nothing),
keeps the current release as `.app-previous`, switches `app/` and runs
`docker compose up -d`. The observer restarts; the collector only when its
own version changed, so recording normally goes on without a gap. `.env`,
`data/`, `docker-compose.override.yml` and the database are never touched.

**Coming from 0.5.0 or older** (a folder per version, data in a Docker
volume), once:

```bash
tar xzf heatpumpsn-observer-0.6.0.tar.gz
heatpumpsn-observer-0.6.0/scripts/update.sh --dir ~/heatpumpsn-observer --from ~/heatpumpsn-observer-0.5.0
```

It takes the old `.env`, copies the data volume into `data/` (the volume is
kept) and starts everything from the install folder; the collector restarts
once for this move.

**Roll back** to the release before the last update:

```bash
cd ~/heatpumpsn-observer && mv app .app-broken && mv .app-previous app && docker compose up -d
```

**Clean up** after a good update:

```bash
~/heatpumpsn-observer/app/scripts/cleanup.sh            # lists what is not needed
~/heatpumpsn-observer/app/scripts/cleanup.sh --yes      # deletes it (never .env, data/ or the database)
```

## 4. Keep it running

* **Backup**: `.env` and `data/` plus a database export (Status tab →
  *Export*, or `docker compose exec observer observer db export`; the file
  lands in `data/exports/`).
* **When something fails**: *Messages* at the top of the portal keeps what
  the page showed; *Settings → Log* shows the server's log
  (`data/logs/observer.log`); the collector's log is
  `docker compose logs collector`.
* **Never** run `docker compose down -v`: `-v` deletes the InfluxDB volumes,
  all recorded data.
