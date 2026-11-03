# Deploying the observer (release)

**Making a release** (development machine): raise `version` in
`pyproject.toml`, add a CHANGELOG entry, commit, then

```bash
scripts/release.sh --tag            # tests, dist/heatpumpsn-observer-<version>.tar.gz, tag observer-v<version>
git push origin observer-v<version> # GitHub Actions: tests, bundle, image build, GitHub Release with the bundle
```

Two containers: **InfluxDB 2.7** (the recorded samples) and the **observer**
(ingest from the home MQTT broker, portal, house understanding, library,
exports). Development keeps running on the development machine; the release
machine starts collecting on its own and receives the history and the
observer's state by export/import.

## 1. Install

A release is the bundle `heatpumpsn-observer-<version>.tar.gz`: the observer
package, the Dockerfile, this `deploy/` folder, README and CHANGELOG. Get it
from the GitHub release (`observer-v<version>`; for a private repository with
`gh release download observer-v0.2.0 -R gume/HeatpumpsN`) or copy it from
the development machine (`observer/dist/`, built by `scripts/release.sh`).

(Building from a repository checkout instead of a bundle: run
`scripts/native.sh` in `observer/` first; it copies the controller's MPC core
into `native/`, which the image compiles.)

On the release machine (Docker with the compose plugin):

```bash
tar xzf heatpumpsn-observer-0.2.0.tar.gz
cd heatpumpsn-observer-0.2.0/deploy
cp env.example .env
# fill in .env: INFLUX_ADMIN_PASSWORD and INFLUX_ADMIN_TOKEN (openssl rand -hex 32),
# OBSERVER_MQTT_HOST/PORT (the home broker), OBSERVER_PORTAL_PASSWORD, TZ
docker compose up -d --build
docker compose ps            # all three healthy after about a minute
docker compose logs observer # "MQTT connected", "OBSERVER_INFLUX_READ_TOKEN: created"
```

On the first start InfluxDB creates the org and the bucket (unlimited
retention), and the observer creates `/data` from the release configuration
(`observer/defaults/config.release.yaml`) and its own **read-only token**.
Collection starts at once with the generic registry rules.

The portal is published on `127.0.0.1:8090`: reach it with an SSH tunnel
(`ssh -L 8090:127.0.0.1:8090 <machine>`), or set `OBSERVER_BIND=0.0.0.0`
together with `OBSERVER_PORTAL_PASSWORD` (HTTP Basic, user
`OBSERVER_PORTAL_USER`). The portal can change the registry and import data:
never expose it without the password.

## 2. Bring the observer's state and the history

On the **development** observer (portal, Status tab, "Database export and import"):

1. **Export state (.tgz)**: registry (which signals are stored and what they
   mean), catalog, house understanding (review, owner knowledge, accepted
   versions, draft), house library items, audit flags, model settings, the
   house context. No secrets, no recorded data.
2. **Export** (database): one Parquet file with all recorded samples
   (about 2 bytes per sample), downloadable from the list.

On the **release** observer (same section):

1. **Import state…**: replaces registry, house, library and settings (the
   previous files go to `/data/backup/<time>/`); the ingest uses the new
   registry at once.
2. **Import a file…** with *dry run* unchecked: writes the samples the
   release database does not have. Samples already present (same signal,
   same timestamp) are skipped, so the overlap with what the release machine
   has collected meanwhile, or a second import, never duplicates anything.
   Nothing is deleted.

The same works from the command line inside the container:

```bash
docker compose cp observer-state.tgz observer:/data/exports/
docker compose exec observer observer state import /data/exports/observer-state.tgz
docker compose cp observer-20261102-153653.parquet observer:/data/exports/
docker compose exec observer observer db import /data/exports/observer-20261102-153653.parquet
```

## 3. Optional

* **LLM**: `OBSERVER_LLM_BASE_URL` / `OBSERVER_LLM_KEY` in `.env` (a proxy on
  the Docker host: `http://host.docker.internal:<port>/v1`), then the models
  per task on the Settings page.
* **Library** (the analysis tools and skills, repository
  gume/observer-library): Library tab → *Import from URL* with the release
  asset, e.g.
  `https://github.com/gume/observer-library/releases/download/v0.2.0/observer-library-0.2.0.tgz`
  (or `docker compose exec observer observer library import <url>`).
* **LLM providers**: `OBSERVER_LLM_BASE_URL` / `OBSERVER_LLM_KEY` (and
  `OBSERVER_LLM_2_BASE_URL` / `_KEY` … up to 9) in `deploy/.env`, then
  `docker compose up -d`; they appear on the Settings page, where the model
  per task is chosen. "localhost" inside the container is the container
  itself: a proxy on the Docker host is `http://host.docker.internal:<port>/v1`,
  on another machine its address (and it must listen there, not only on
  127.0.0.1).
* **Agent sandbox** (agent runs, tool tests): the image contains bubblewrap
  and the sandbox's Python environment, but Docker's default security
  profiles forbid the namespaces bubblewrap creates. Allow them for the
  observer container with the extra compose file:

  ```bash
  echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.sandbox.yml' >> .env
  docker compose up -d
  docker compose exec observer observer sandbox-check
  ```

  `sandbox-check` must end with `sandbox OK` and report `network: none`, no
  visible `.env`, no tokens and an enforced memory limit. What it costs: the
  observer container runs without Docker's seccomp and AppArmor profiles
  (still as a non-root user, without extra capabilities); every piece of
  agent code runs inside bubblewrap without network, sees only its workspace
  and read-only libraries, and is limited in memory, processes, CPU time and
  file size. `OBSERVER_MEM_LIMIT` (default 3g) caps the whole container. On
  hosts that forbid unprivileged user namespaces altogether (some kernels'
  `kernel.unprivileged_userns_clone=0` or Ubuntu's AppArmor restriction),
  `sandbox-check` says so; data collection and everything else work
  regardless.

## 4. Operate

* **New versions**: the portal shows a banner when a newer observer or
  library-pack release exists (checked every 12 hours; Library tab → *Check
  for updates*). A newer pack installs with one click from the banner.
* **Containers**: `collector` records (MQTT in, InfluxDB out, nothing else),
  `observer` is the portal (web, alerts, agents, parameters); both run the
  same image and share `/data`. The portal shows the collector on the Status
  tab and raises an alarm when it stops reporting.
* **Update only the portal** (no gap in the data):
  `docker compose up -d --build observer`. The collector keeps running on the
  old version; a later full `docker compose up -d` replaces it as well (a few
  seconds without recording). Coming from 0.4.0, the first full update
  creates the collector.
* **Stored samples** on the Status tab is counted from the database once
  and then follows what the collector writes; *Recount* counts again (it also
  runs after every import).
* **Update** to a new observer release: unpack the new bundle next to the old
  one, copy `.env` over (`cp ../../heatpumpsn-observer-0.2.0/deploy/.env .`;
  coming from 0.2.0, delete its line `OBSERVER_VERSION=0.2.0`), and run
  `docker compose up -d --build` in its `deploy/`. The compose project
  name is fixed (`heatpumpsn-observer`), so the new containers use the same
  volumes (`heatpumpsn-observer_influx-data`, `_influx-config`,
  `_observer-data`): the data and the state stay. The portal header and
  `/healthz` show the running version. Read the CHANGELOG first.
* **Backup**: Status tab → Export, or
  `docker compose exec observer observer db export` (the file lands in
  `/data/exports/`; `docker compose cp observer:/data/exports/<file> .`).
  A state export alongside keeps the registry and the house understanding.
* **InfluxDB UI**: `http://127.0.0.1:8086`, user `INFLUX_ADMIN_USER`.
* **Never** run `docker compose down -v` on the release machine: `-v`
  deletes the volumes, i.e. all recorded data.

Checked on the development machine with a separate test project: first
start (setup, read token, MQTT, storing), state import (102 stored signals
from the development registry), import of 1,003,897 samples, a second
import writing 0, restart with everything kept.
