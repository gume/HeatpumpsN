# Observer changelog

Releases are git tags `observer-v<version>` (the version in `pyproject.toml`).
Each release has a deploy bundle `heatpumpsn-observer-<version>.tar.gz`
(see `deploy/README.md`).

## 0.5.0 - 2026-11-03

Data collection in its own container (portal updates without a gap), a
Recount of the stored samples, learning the mixer model from live data with a
signed parameter path, a closed-loop backtest of every proposal with the
controller's own MPC core, and that simulation for the agent (`obs.sim`).

Update: the first full `docker compose up -d --build` creates the
`collector` container next to `observer`; after that, update the portal
alone with `docker compose up -d --build observer`. Sending parameter sets
stays off until `parameters.push_enabled` is set in config.yaml.

* Learning from live data (plan: identification and parameter path): the
  controller's mixer model (law, transport delay, sensor lags; per pump
  scenario) is re-identified from recorded data (`identify.py`, a copy of the
  firmware model, checked step by step against it), daily (check type
  `identify`) or on the new Parameters tab. The current set is compared with
  the re-identified one on held-out data; only a clearly better model becomes
  a proposal, and a large gap raises a drift alert. Proposals show errors,
  parameter changes, the law with the data coverage and a held-out plot.
* Parameter path: an approved proposal sends only the model fields to
  `cmd/mpc/params`, HMAC-signed with `OBSERVER_CONTROLLER_SECRET`; the
  controller's acknowledgement marks it accepted or rejected; every sent set
  keeps the one it replaced for a roll back. Sending is off until
  `parameters.push_enabled` is set.
* Data collection and the portal are separate containers (`observer
  collect`, `observer serve --no-ingest` / `OBSERVER_INGEST=external`):
  `docker compose up -d --build observer` updates the portal without a gap
  in the recording. The portal shows the collector's state
  (`data/collector.json`) and raises an alarm when it is silent for 90 s or
  loses the broker. `observer serve` alone still does both (development).
* Stored samples: *Recount* button; the count also runs again after an import
  and follows a collector restart. MQTT client ids are unique per process.
* Simulation package `observer/sim/`: the firmware's MPC core compiled for
  the host (in a build stage of the image; on first use in development),
  the identified house plant and boiler (copies from the floor simulator,
  checked for parity) and the closed loop with its metrics. The release
  bundle carries the core's sources in `native/` (`scripts/native.sh`).
* Closed-loop backtest of a parameter proposal (`sim/replay.py`): recorded
  situations replayed with the controller's MPC core, current vs proposed
  set, each on the re-identified and on the currently assumed mixer; error
  to the setpoint, overshoot, time to the setpoint, travel, motor starts and
  fallbacks on the proposal card, with each situation and an example chart,
  and a verdict. Runs with every new proposal and on demand. The recorded
  floor setpoint joins the identification data.
* The simulator for the agent: `obs.sim` in the sandbox (`closed_loop`,
  `replay` of a recorded stretch, `compare`, `current_sets`), with the
  observer package and the compiled MPC core mounted read-only; data-socket
  operation `mpc_params`. The library's `mixer_what_if` skill and
  `control_metrics` tool go with it.
* The controller's slow log (exact model inputs) is stored now (registry
  rule); until enough of it exists, identification uses the published values
  and says so.

## 0.4.0 - 2026-11-03

Supervision with Home Assistant alerts, editors for harvested tools and
skills, limited LLM waits with a choice to retry, wait or switch the model.

* Portal: the menu bar stays at the top (one line, tabs scroll on narrow
  screens); *Stored samples* shows the database total (counted once at the
  start, then raised by every write); refreshes keep opened steps and typed
  text; a development checkout shows its git state in the version.

* LLM answers have a limit (Settings: *Response limit*, default 120 s): with
  streaming (on by default) it is the longest silence while the answer
  arrives, without streaming the whole wait. A request that runs into the
  limit is not repeated. "Model not available" (quota used up, rate limit,
  busy, no answer in time, unreachable, model or key rejected) is one kind of
  error everywhere: a dialog offers *Retry now*, *Wait and retry* (when the
  provider names a time within 15 minutes) and *Use another model for this
  task* (saved in Settings), for the house understanding, descriptions,
  proposals, the harvest, writing tool code and paused agent runs.
* The Proposals tab shows the signal proposals again (a name clash with
  the library inbox had replaced its content).

* Supervision (plan phase C): automatic checks open and close alerts.
  *Events* on every message (controller errors and warnings per code, mixer
  control leaving MPC mode, EMS-ESP offline, a boiler maintenance message),
  *fast checks* on a schedule (controller, EMS-ESP, encoder or room sensors
  silent; supply temperature too high; rooms out of range), *slow checks*
  from the database (temperatures stuck for hours) and optional *scheduled
  agent runs* (a skill; findings become alerts; off by default). Checks live
  in `checks.yaml` in the data directory (created from the defaults,
  editable on the new Alerts tab, part of the state export). Alerts open
  once, count repeats, close by themselves and can be acknowledged; they
  are published retained on `observer/alerts` with Home Assistant discovery
  ("Heating problem", "Heating alerts", availability via a last will).

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
