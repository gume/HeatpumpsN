# Deploying the observer (release)

The short install and update guide is [`../INSTALL.md`](../INSTALL.md); this
page has the details.

**Making a release** (development machine): raise `version` in
`pyproject.toml` and the observer image tag in `deploy/docker-compose.yml`,
add a CHANGELOG entry, commit, then

```bash
scripts/release.sh --tag            # checks, tests, dist/heatpumpsn-observer-<version>.tar.gz, tag observer-v<version>
git push origin observer-v<version> # GitHub Actions: tests, bundle, both images, GitHub Release with the bundle
```

When the collector's files changed (`observer/collector.py` `FILES`,
`Dockerfile.collector`, `collector-requirements.txt`), raise
`COLLECTOR_VERSION` in `observer/collector.py` and run
`python scripts/collector_lock.py` (it records the files' hash in
`collector.lock` and in the compose file); the tests and `release.sh` refuse
a change without a new collector version. Keep the collector unchanged
whenever possible: then an update never interrupts the recording.

Three containers:

| container | image | version | job |
|---|---|---|---|
| `influxdb` | `influxdb:2.7.12` | fixed | the recorded samples (named volume) |
| `collector` | `heatpumpsn-collector:<v>` (`Dockerfile.collector`) | its own, e.g. 1.0.0 | MQTT in, InfluxDB out, nothing else |
| `observer` | `heatpumpsn-observer:<v>` (`Dockerfile`) | the release | portal, alerts, agents, parameters, imports and exports |

An update replaces the observer; the collector only when its version
changed, otherwise it keeps running (the update says so).

## 1. The install folder: yours and code

Everything lives in one folder that keeps its name across updates, e.g.
`~/heatpumpsn-observer`:

| path | whose | what |
|---|---|---|
| `.env` | **yours** | passwords, tokens, broker, ports, LLM providers; never replaced |
| `data/` | **yours** (the observer writes it) | `/data` in the containers: `config.yaml`, `registry.yaml` (what is stored and what it means), `checks.yaml`, `house/` (house understanding, notes), `library/` (installed packs, your tools and skills, audits), `data/` (catalog, alerts, proposals, parameter sets, settings), `runs/` (agent runs), `exports/` (database and state exports), `backup/`, and `.env` with what the observer generates itself (its read-only InfluxDB token) |
| `docker-compose.override.yml` | **yours**, optional | your own compose additions (compose reads it automatically) |
| InfluxDB volume `heatpumpsn-observer_influx-data` | **yours** | the recorded samples (and `_influx-config`) |
| `app/` | code | the release bundle: replaced by every update, never edit it |
| `docker-compose.yml`, `docker-compose.sandbox.yml` | code | links into `app/deploy/` |
| `.app-previous/` | code | the release before the last update (roll back); `cleanup.sh` removes it |

Back up `.env` and `data/` (plus a database export) and you have everything
that is not code.

The containers write `data/` as uid 1000. If your user has another uid, put
`OBSERVER_UID=<id -u>` and `OBSERVER_GID=<id -g>` into `.env` so that the
files in `data/` are yours (the update script adjusts the ownership).

## 2. Install, update, clean up

A release is the bundle `heatpumpsn-observer-<version>.tar.gz`: the observer
package, both Dockerfiles, `deploy/`, `scripts/update.sh` and `cleanup.sh`,
the MPC core sources (`native/`), README and CHANGELOG. Get it from the GitHub
release (`observer-v<version>`; for a private repository with
`gh release download observer-v<version> -R gume/HeatpumpsN`) or copy it
from the development machine (`observer/dist/`).

**First install** (Docker with the compose plugin):

```bash
tar xzf heatpumpsn-observer-0.6.1.tar.gz
heatpumpsn-observer-0.6.1/scripts/update.sh --dir ~/heatpumpsn-observer
# .env is created from the example: fill it in - INFLUX_ADMIN_PASSWORD and
# INFLUX_ADMIN_TOKEN (openssl rand -hex 32), OBSERVER_MQTT_HOST/PORT (the home
# broker), OBSERVER_PORTAL_PASSWORD, TZ - then
cd ~/heatpumpsn-observer && docker compose up -d
docker compose ps            # all three healthy after about a minute
```

**Coming from 0.5.0 or older** (a folder per version, data in the volume
`heatpumpsn-observer_observer-data`): once,

```bash
tar xzf heatpumpsn-observer-0.6.1.tar.gz
heatpumpsn-observer-0.6.1/scripts/update.sh --dir ~/heatpumpsn-observer --from ~/heatpumpsn-observer-0.5.0
```

It takes `.env` from the old folder, builds the images, stops the old
observer and collector, copies the volume's content into `data/` (the volume
is kept), and starts everything from the install folder. InfluxDB keeps its
volume and runs on. If your old `.env` has
`COMPOSE_FILE=docker-compose.yml:docker-compose.sandbox.yml`, it works
unchanged (both names are links in the install folder).

**Every later update** (from the install folder):

```bash
~/heatpumpsn-observer/app/scripts/update.sh heatpumpsn-observer-0.7.0.tar.gz
```

It builds the new images first (a failing build changes nothing), keeps the
current code as `.app-previous`, switches `app/`, and runs `docker compose
up -d`: the observer restarts, the collector only when its version changed.
`.env`, `data/` and your override are never touched. Read the CHANGELOG
first; the portal header and `/healthz` show the running version.

**Roll back** (the code; a newer version may have changed files in `data/`
in a way an older one does not know - the CHANGELOG says so when it does):

```bash
cd ~/heatpumpsn-observer && mv app .app-broken && mv .app-previous app && docker compose up -d
```

**Clean up**:

```bash
~/heatpumpsn-observer/app/scripts/cleanup.sh                 # lists what is surely not needed
~/heatpumpsn-observer/app/scripts/cleanup.sh --yes           # deletes it
~/heatpumpsn-observer/app/scripts/cleanup.sh --old-volume    # also lists the pre-0.6.0 data volume
```

Candidates: `.app-previous` and leftovers, bundle archives in and next to the
install folder, old unpacked bundle folders next to it (kept when their
`.env` differs from yours), observer and collector images no container
uses, their dangling build layers, and with `--old-volume` the pre-0.6.0
data volume (only once it was copied to `data/`). Never `.env`, `data/`,
the override or the InfluxDB volumes.

On the first start InfluxDB creates the org and the bucket (unlimited
retention), and the observer creates `data/` from the release configuration
(`observer/defaults/config.release.yaml`) and its own **read-only token**.
Collection starts at once with the generic registry rules.

The portal is published on `127.0.0.1:8090`: reach it with an SSH tunnel
(`ssh -L 8090:127.0.0.1:8090 <machine>`), or set `OBSERVER_BIND=0.0.0.0`
together with `OBSERVER_PORTAL_PASSWORD` (HTTP Basic, user
`OBSERVER_PORTAL_USER`). The portal can change the registry and import data:
never expose it without the password.

(Building from a repository checkout instead of a bundle: run
`scripts/native.sh` in `observer/` first; it copies the controller's MPC core
into `native/`, which the image compiles.)

## 3. Bring the observer's state and the history

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

## 4. Optional

* **LLM**: `OBSERVER_LLM_BASE_URL` / `OBSERVER_LLM_KEY` in `.env` (a proxy on
  the Docker host: `http://host.docker.internal:<port>/v1`), then the models
  per task on the Settings page.
* **Library** (the analysis tools and skills, repository
  gume/observer-library): Library tab → *Import from URL* with the release
  asset, e.g.
  `https://github.com/gume/observer-library/releases/download/v0.2.0/observer-library-0.2.0.tgz`
  (or `docker compose exec observer observer library import <url>`).
* **LLM providers**: `OBSERVER_LLM_BASE_URL` / `OBSERVER_LLM_KEY` (and
  `OBSERVER_LLM_2_BASE_URL` / `_KEY` … up to 9) in `.env`, then
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

## 5. Operate

* **New versions**: the portal shows a banner when a newer observer or
  library-pack release exists (checked every 12 hours; Library tab → *Check
  for updates*). A newer pack installs with one click from the banner.
* **Stored samples** on the Status tab is counted from the database once
  and then follows what the collector writes; *Recount* counts again (it also
  runs after every import). The Status tab shows the collector (its version,
  connection, last write) and an alarm is raised when it stops reporting.
* **Updates**: section 2 (`app/scripts/update.sh`).
* **Backup**: `.env` and `data/` (a plain copy, best while the portal is
  stopped) plus a database export: Status tab → Export, or
  `docker compose exec observer observer db export` (the file lands in
  `data/exports/`).
* **InfluxDB UI**: `http://127.0.0.1:8086`, user `INFLUX_ADMIN_USER`.
* **Never** run `docker compose down -v` on the release machine: `-v`
  deletes the InfluxDB volumes, i.e. all recorded data.

Checked on the development machine with a separate test project: first
start (setup, read token, MQTT, storing), state import (102 stored signals
from the development registry), import of 1,003,897 samples, a second
import writing 0, restart with everything kept.
