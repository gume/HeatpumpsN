# Observer changelog

Releases are git tags `observer-v<version>` (the version in `pyproject.toml`).
Each release has a deploy bundle `heatpumpsn-observer-<version>.tar.gz`
(see `deploy/README.md`).

## Unreleased

* Harvest ("Find tools and skills in this run") runs in the background: the
  run view shows how long the model has been working, the error if it
  fails, and the proposals when it is done. It no longer looks stuck with a
  slow (thinking) model.
* Proposals wait in the Library tab (*Proposed by agent runs*) and open in
  editors:
  * skill editor: name, title, description, the tools it uses, the recipe as
    Markdown with a preview;
  * tool editor without code: goal, the data it needs, the inputs as a
    table, the output, and the tests as plain sentences. Changing the
    description marks the code as out of date; *Write code from the
    description* lets the model (task *harvest*) write code and tests,
    which then run in the sandbox. The generated code is shown read-only.
  * Save draft, Accept into the library (unaudited), Dismiss.
* Tool manifests keep the test sentences (`tests` in tool.yaml); the
  Library tab shows them.

## 0.3.1 - 2026-11-02

* LLM providers set in the data directory's `.env` were hidden by the empty
  variables docker compose passes: an empty environment variable no longer
  wins over `.env`. The compose file passes the whole `deploy/.env` to the
  observer, so providers 2-9 (`OBSERVER_LLM_<n>_BASE_URL/KEY`) and
  `OBSERVER_GITHUB_TOKEN` arrive too.
* Agent sandbox in the container: the image brings bubblewrap and the
  sandbox's Python environment (numpy, pandas, scipy, matplotlib, pytest);
  `deploy/docker-compose.sandbox.yml` allows the namespaces it needs
  (seccomp and AppArmor unconfined for the observer container, memory cap
  for the container). The sandbox runs without its own /proc where the
  container refuses that mount, sees the dynamic linker's cache, and limits
  memory by address space without systemd.
* `observer sandbox-check`: network, secrets, memory limit, packages.

## 0.3.0 - 2026-11-02

* Update check: the observer looks at the GitHub releases of the observer
  (`observer-v*` in gume/HeatpumpsN) and of every installed library pack
  (the repository named in its `library.yaml`) every 12 hours and on demand;
  a banner in the portal shows newer versions (`observer updates` on the
  command line). Nothing is installed automatically.
* Library packs from a URL: Library tab *Import from URL* and
  `observer library import <url>`; a newer pack release is installed with one
  click from the banner.
* Configuration: `updates` (enabled, observer_repository, interval_h);
  `OBSERVER_GITHUB_TOKEN` for private repositories.
* `OBSERVER_VERSION` is no longer in `deploy/env.example`: the compose file
  of each bundle carries its version. When upgrading from 0.2.0, delete the
  line `OBSERVER_VERSION=0.2.0` from the copied `.env`.

## 0.2.0 - 2026-11-02

First release for deployment.

* Ingest from MQTT (EMS-ESP, the controller, MixerMeter, Zigbee2MQTT, Home
  Assistant Statestream), signal registry and catalog, raw storage in
  InfluxDB.
* Portal: status, signals (bulk edit), LLM proposals, house understanding
  (review, owner knowledge, accepted versions with archive), agent runs,
  library (tools and skills with versions, tests, audit flags, export and
  import), settings (LLM provider and model per task).
* Agent with read-only tools, a bubblewrap sandbox with a read-only data
  socket, harvest of tools and skills from runs, pause and resume on LLM
  quota limits (not available in the container yet).
* Database export and import as one Parquet file, import without
  duplicates; state export and import (registry, house, library, settings).
* Release deployment: Dockerfile, docker compose with InfluxDB, settings
  from the environment, read-only token created at start, optional portal
  password.
