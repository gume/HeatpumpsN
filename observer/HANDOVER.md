# HeatpumpsN observer — handover

Entry point for continuing the observer in a new session. Updated 2026-11-09.
**Start with §2.3 (state on 2026-11-09) and §4 (next steps).**
Open work is in [`ISSUES.md`](ISSUES.md); the roadmap in
[`../plans/observer_agentic_plan.md`](../plans/observer_agentic_plan.md); usage
in [`README.md`](README.md). The controller has its own handover:
[`../firmware/HANDOVER.md`](../firmware/HANDOVER.md).

## 0. Working agreements (read first)

1. **Never clear the observer's InfluxDB** (reset script, `docker compose down
   -v`, deleting the volume, dropping data) without the owner's prior
   agreement: it holds recorded house data. The reset script refuses without
   `--yes-delete-all-data`. Schema or tag changes that need a rebuild are
   proposed first, with what would be lost.
2. **Operational data never goes into git.** Code, docs and generic defaults
   do; everything of the running installation (registry, house description and
   notes, catalog, settings, configs, secrets) lives in the data directory.
3. **The observer never commands the plant.** Its outputs are reports, alerts
   and proposed parameter sets the owner approves (`../plans/master_plan.md`).
4. Commit with the attribution line; **do not push** unless asked.

## 1. What the observer is

A Python service that records every MQTT signal of the house (controller,
EMS-ESP boiler, MixerMeter, Zigbee2MQTT, Home Assistant states) into its own
InfluxDB, lets the owner describe and select signals in a web portal, and uses
an LLM (optional) to propose descriptions and a reviewed description of the
house. Next it supervises the plant and, later, investigates anomalies and
optimises parameters with agents.

**`registry.yaml` in short:** the list of which signals matter and what they
mean. `rules` are glob patterns covering many signals at once (e.g. every
`ha_state/sensor/*_temperature/state` is a room temperature in °C; all
`zigbee2mqtt/*` not stored); `signals` are exact entries for one signal that
add to or override its rule (name, floor, circuit, store or not). The portal's
Signals tab writes the entries. The generic starter file is
`observer/defaults/registry.yaml`; the house's own is in the data directory.

## 2. Where things stand

* **Code** in `observer/` (git). **Operational data** in the data directory
  `OBSERVER_HOME`, default `~/.local/share/heatpumpsn-observer` (never in git):
  `config.yaml`, `.env` (secrets: InfluxDB token, LLM endpoints and keys),
  `registry.yaml` (this house's sensor selection and descriptions),
  `house/` (your review, owner knowledge, accepted understanding and model, archive), `data/` (catalog,
  settings, proposals, log). `observer init` creates a data directory from
  `observer/defaults/`.
* **Running (development, this machine):** two processes from the checkout's
  venv since 0.5.0: `observer collect` (MQTT → InfluxDB, started 2026-11-03
  10:54) and `observer serve --no-ingest` (portal `http://127.0.0.1:8090/`,
  HTTP Basic password from `OBSERVER_PORTAL_PASSWORD`). Start each with
  `setsid nohup ~/Work/HeatpumpsN/observer/.venv/bin/observer <collect | serve
  --no-ingest> > ~/.local/share/heatpumpsn-observer/data/<collect|serve>.log 2>&1 &`.
  Restart **only the portal** after code changes (`kill <pid of serve>`, find it
  with `pgrep -a observer`; not `pkill -f`, which matches its own shell): the
  collector keeps recording. Restart the collector only for collector changes
  (`COLLECTOR_VERSION`), it leaves a gap in the data.
* **Database:** InfluxDB 2.7 container `observer-influx-dev`
  (`observer/docker-compose.dev.yml`, volume `observer-influx-dev`, port 38086,
  org `heatpumpsn`, bucket `observer`). Holds recorded house data: see §0.1.
* **Data path in development:** the home broker (Mosquitto 1.6 container on
  the Jetson, `/home/jetson/services/homeassistant`) bridges `heatpumpsn/#`,
  `ems-esp/#`, `mixermeter/#`, `zigbee2mqtt/#`, `homeassistant/#` and
  `ha_state/#` (HA MQTT Statestream, `mqtt_statestream.yaml` included from
  HA's configuration) outward under `mirror/` to `localhost:31883` here,
  through the systemd service `mqtt-mirror-tunnel` on the Jetson (autossh,
  key `/var/lib/mqtt-mirror/id_ed25519`, restricted in `~gume/.ssh/authorized_keys`
  to the broker port). Direction `out` only.
* **LLM:** provider slot 1 "Gemini proxy" (`http://localhost:38080/v1`, in the
  data directory's `.env`); all three tasks on `gemini-3.5-flash-lite`. The
  house description is thin with that model. The owner has
  `gemini-3.8-flash` too, but only 20 requests a day: fine for the house
  task (2 requests per draft), not for agent runs (10–25 requests).
  `gemini-2.5-pro` answers 404, the pro models 429. All tasks are on
  flash-lite now (the owner's choice, 2026-11-02). Since 0.6.0 the House tab
  can use another model for one draft without changing the settings.
* **House understanding** (three parts: your review, owner knowledge,
  understanding): a draft from before the knowledge step is waiting; the
  owner's review (notes.md) has content that "Generate a draft" will
  consolidate. **Nothing accepted yet**; supervision from the house model and
  the agents wait for it.
* **Next:** §4.


### 1.8 Releases (2026-11-02)

* Observer: tags `observer-v<version>` on gume/HeatpumpsN (public), GitHub
  Release with the deploy bundle by `.github/workflows/observer-release.yml`;
  releases up to 0.5.0 published; 0.6.0 see §2.2. Release steps: CHANGELOG
  entry, version in `pyproject.toml` and the image in
  `deploy/docker-compose.yml`, commit, `scripts/release.sh --tag` (tests,
  bundle in `dist/`, annotated tag), the owner pushes the branch **and the
  tag** (`git push origin main observer-v<version>`; a plain push leaves the
  tag behind and the workflow does not run). Release only when worthwhile,
  not per small change.
* Library: gume/observer-library (public), tags `v<version>`, workflow
  publishes the pack; v0.3.0 is the latest (control_metrics, mixer_what_if).
* The observer checks both for newer releases (banner; packs install with
  one click). Pushing stays with the owner.

### 1.9 Release deployment (2026-11-02)

`observer/Dockerfile`, `observer/deploy/` (compose, env.example, README with
the migration steps). Tested on this machine as a separate compose project
(removed afterwards, with its volumes). The owner deploys it on another
machine and moves state and history there with the state and database
export/import; development continues here. Agent runs do not work in the
container yet (sandbox).

### 2.0 Database export and import (2026-11-02)

`observer/dbarchive.py`; Status tab and `observer db export|import`. One
Parquet+zstd file (~2 bytes/sample), import writes only samples whose
(signal, field, timestamp) is missing, never deletes. (The round-trip test
bucket was removed on the owner's request.)

### 2.1 Agent, sandbox, data socket, library (2026-11-02)

* Design and use: `README.md` → "Agent, sandbox, data socket and library".
  Portal tabs **Agent** (runs, harvest, pause/resume) and **Library** (tools
  and skills, tests, audit flags); CLI `observer agent | library | test-tool |
  mcp`.
* The agent picks its data and windows; code in the bwrap sandbox (no
  network) reads InfluxDB through the run's Unix-socket data service
  (`agent/broker.py`, read-only token `OBSERVER_INFLUX_READ_TOKEN`, every
  request in `queries.jsonl`). Sandbox Python: `<OBSERVER_HOME>/sandbox/venv`
  (`observer sandbox-setup`; pytest is needed for tool tests).
* Library: the tools and skills live in their own repository,
  **`~/Work/observer-library`** (local git, tag v0.2.0, no remote yet;
  `scripts/check.py`, `scripts/pack.sh`), configured in the data directory's
  config.yaml as `agent.library_paths`. House items, history, packs,
  `audits.yaml` / `tests.yaml` in `<OBSERVER_HOME>/library/`. 13 tools
  (incl. room analysis: temperature_segments, cooling_fit, room_rc_fit,
  heatup_events, comfort_stats, daily_profile, degree_hours) and 8 skills
  (incl. room_heat_loss, room_heat_up, room_comfort, emitter_sizing).
  Nothing is audited yet: the owner audits in the Library tab.
* Models: all tasks on `gemini-3.5-flash-lite` (owner's choice;
  `gemini-3.8-flash` has 20 requests/day, worth it for *harvest* and
  *house*). Flash-lite follows recipes and the library poorly (it wrote its
  own code instead of using the skill and tools); a stronger agent model
  should do better.
* A harvest proposal (`identify_short_cycles`, overlapping `on_periods`)
  waits in run `20261102-024337-question`, not added.

### 2.2 State on 2026-11-04 (releases 0.4.0 – 0.6.0)

* **Releases:** 0.4.0 (supervision, alerts to HA, tool and skill editors,
  limited LLM waits), 0.5.0 (collector process, mixer model identification,
  signed parameter path with rollback, closed-loop backtest with the
  controller's MPC core, `obs.sim`), 0.6.0 (one install folder with
  `scripts/update.sh` / `cleanup.sh`, the collector in its own image with
  `COLLECTOR_VERSION` 1.0.0 and `collector.lock`, regrouped portal tabs, a log
  file, searchable model fields). Details: `CHANGELOG.md`.
* **Git:** `main` (bea86c6) is pushed. The tag **`observer-v0.6.0` was not
  pushed yet** (checked 2026-11-04): `git push origin observer-v0.6.0` starts
  the release workflow. The bundle is in `dist/heatpumpsn-observer-0.6.0.tar.gz`.
* **GitHub SSH:** GitHub removes `ssh-rsa` (SHA-1) and
  `diffie-hellman-group-exchange-sha256` (brownouts 2026-11-04 and
  2026-12-09, removal 2027-01-13). The owner's client (Gume-Zen) now pushes
  with an ed25519 key (`~/.ssh/github_ed25519`); magnum has no key of its own
  and pushes through the forwarded agent (`ssh -A`). A tmux pane keeps a stale
  `SSH_AUTH_SOCK` after a reconnect: push from a fresh shell.
* **Portal (0.6.0):** tabs Status, Alerts, Signals (with the LLM's proposed
  descriptions), Agent (House, Analyses, Library), Parameters, Settings (LLM,
  Rules, Log). The tab is in the address (`#house`, `#log`). Every message the
  page showed is under *Messages* at the top (this browser tab only); the
  server's log is `<data dir>/logs/observer.log` (rotating, Settings → Log,
  `GET /api/log`), in the release `data/logs/observer.log`.
* **Development portal runs old code:** the `serve --no-ingest` process was
  started 2026-11-03 19:34, before the 0.6.0 portal changes (no `logs/`
  folder in the data directory yet). Restart the portal (not the collector)
  to see them. The 0.6.0 page was checked with the API and a script, **not
  clicked through in a browser**: do that after the restart.
* **Parameter path:** sending is off (`parameters.push_enabled` is not set in
  the data directory's config.yaml); no set has been sent to the real
  controller yet. The controller's slow log is being recorded; until enough
  exists, identification uses the published values.

### 2.3 State on 2026-11-09 (identification, retry handling, upload prep)

Full findings: [`docs/mixer-identification-2026-11-09.md`](docs/mixer-identification-2026-11-09.md).
Nothing was pushed to the controller.

* **Git:** `main` is 5 commits ahead of `origin/main` (6f0c59e):
  `237de36` mpc three-strike retry block, `d76c841` portal unblock button and
  banner, `b2b4a34` state fallback when a slow-log signal is missing,
  `60e8ecd` identify only the law on the state fallback, `7eb5eaa` mpc doc and
  log-check comment. The owner pushes; no tag/release was made.
* **Slow log is recorded now:** 55 k samples, 2026-11-01 10:46 – 2026-11-09
  15:40, 39.57 h of usable floor segments. The collector hot-reloads
  `registry.yaml`, so a Signals edit needs no restart. The development
  InfluxDB was down for part of 2026-11-09; the collector backfilled after the
  restart (watch for gaps around such outages).
* **Re-identification on the exact slow log:** the free-timing fit is clearly
  better on held-out data (1.346 -> 0.801 K) but the closed-loop backtest says
  **not recommended** (twice the starts/reversals). Keeping the current timing
  and fitting only the law is **neutral** (0.873 -> 0.782 K on the current
  plant, more movement). **Current parameters stay.** Proposal `edd054c886`
  was left in Parameters with the not-recommended backtest; do not approve it.
* **State fallback fixed:** published values are 10 s means of rounded
  signals and cannot identify delay/lags; `identify(fixed_timing=True)` now
  fits only the law when the slow log is too short (test added).
* **Boiler setpoint 70 -> 60 °C on 2026-11-08:** pre-change data (33.8 h) says
  the current model is as good; post-change data (5.7 h) hints at a different
  law but is too short. Re-identify after several days at 60 °C.
* **Storage gap:** `ems-esp/boiler_data#heatingtemp` (boiler heating-water
  setpoint) is still `store: false` in this data directory's `registry.yaml`
  (last modified 2026-11-03), so the 70 -> 60 change has no history in the
  database (only the catalog's current value, 60). Set it to `store: true` if
  the setpoint change should be visible next time.
* **Development portal restarted** 2026-11-09 15:22 with the current code
  (the collector kept running). InfluxDB `observer-influx-dev` was restarted
  after it was found down; the volume was not touched.
* **Uploads prepared, nothing sent:** the S2 needs the firmware image (retry
  strikes + unblock API) and the filesystem image (`app.js.gz`); a parameter
  push would use `cmd/mpc/params` and needs `parameters.push_enabled` plus
  `OBSERVER_CONTROLLER_SECRET`. Wait for the owner's explicit go.

## 3. Working on it

| What | Where |
|---|---|
| Observer portal | `http://127.0.0.1:8090/` |
| Observer InfluxDB | `http://localhost:38086`, org `heatpumpsn`, bucket `observer` (token in the data directory's `.env`) |
| MQTT broker (development; home data under `mirror/`) | `localhost:31883` |
| LLM proxy | `http://localhost:38080/v1` |
| Data directory | `~/.local/share/heatpumpsn-observer` (`OBSERVER_HOME`) |

```bash
cd observer
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/observer init        # new data directory from the defaults (never overwrites)
```

Code map: `observer/cli.py` (commands, data directory), `ingest.py`,
`payload.py` (topic/JSON → samples), `metadata.py` (HA discovery, Z2M),
`catalog.py`, `registry.py` (ruamel round-trip save keeps comments),
`store.py` (InfluxDB), `llm.py`, `settings.py` (per-task models, provider
slots from `.env`), `service.py`, `web.py`, `static/index.html`;
`agent/` (`data.py` read-only queries, `tools.py`, `sandbox.py`,
`skills.py`, `runner.py` loop and audit record, `manager.py` background
runs, `mcp_server.py`); `skills/<name>/` (recipes and helpers).
Added since: `config.py` (configuration loading, shared with the collector),
`collector.py` (`observer collect`, `COLLECTOR_VERSION`), `supervision.py`
(checks and alerts), `identify.py` (mixer model from recorded data, a copy of
the firmware model), `parameters.py` (proposals, approval, signed push,
rollback), `sim/` (the controller's MPC core compiled for the host via
`mpc_shim.cpp` / `fwcore.py`, plant, boiler, closed loop, `replay.py`
backtest), `dbarchive.py`, `state.py` (state export/import), `updates.py`
(release and pack update checks), `logfile.py` (the log file and
`/api/log`).

## 4. Next steps (2026-11-09)

Status against the plans: [`ISSUES.md`](ISSUES.md). In order:

1. **Review the identification proposal** `edd054c886` (Parameters tab):
   backtest not recommended; dismiss it unless it is wanted as a record.
   Re-run the identification after several days at the 60 °C boiler setpoint
   and compare all data with post-change data.
2. **Record the boiler setpoint**: set
   `ems-esp/boiler_data#heatingtemp` to `store: true` (Signals tab) if the
   70 -> 60 °C change should have a history.
3. **Push the 5 commits** (owner; no tag/release was made). The 0.6.1 release
   still has the tag push from §2.2 to do if wanted.
4. **First parameter push on the real plant**, only with a proposal whose
   backtest is recommended or explicitly accepted: `parameters.push_enabled`,
   `OBSERVER_CONTROLLER_SECRET`, approve, check the controller's
   acknowledgement and a roll back. A firmware upload to the S2 needs the
   owner's explicit go (see `../firmware/HANDOVER.md`).
5. **Accept a house understanding**: generate a draft with a stronger model
   for that one draft (House tab), review, accept. Unblocks checks from the
   house model and makes agent reports trustworthy.
6. **Supervision from the house model** (phase C rest): checks derived from
   the accepted model, drift beyond the mixer, an alert starting an agent
   investigation. The mixer drift alert already fires when the re-identified
   model is much better on held-out data; that alone is not enough to push.
7. **Old HA history** (one year): a converter to the export format for
   `db import`, before HA's InfluxDB integration is removed.
8. Operations: scheduled backups (`db export` + state export), retention or
   downsampling, an "observer alive" entity in HA.
