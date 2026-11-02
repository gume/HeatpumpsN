# HeatpumpsN observer

Supervises the heating system from the outside. Primary goals:

1. **Observe and optimise:** store everything relevant with its real
   timestamps, identify the plant (transport delays, valve gain, coupling),
   propose controller parameters and validate them against recorded data
   before they are pushed (the controller's signed parameter channel).
2. **Find anomalies and alert:** stuck or implausible sensors, devices
   offline, boiler short-cycling, circuits that do not deliver heat, mixer
   drift, the controller's own alarms.

It never commands the plant: no relays, no setpoints. The only thing it may
send later is a validated controller parameter set (plan:
`plans/master_plan.md` §1, §5.3). Its absence changes nothing.

## Where things live

| In the repository (git) | In the data directory (never in git) |
| --- | --- |
| code, tests, docs | `config.yaml`, `.env` (secrets) |
| `observer/defaults/`: config and `.env` templates, a generic `registry.yaml` with family rules only | `registry.yaml`: this house's sensor selection and descriptions |
| `docker-compose.dev.yml`, scripts | `house/`: your review, owner knowledge, accepted understanding, archive |
| `observer/sandbox_lib/`: the `obs` library for sandboxed code (tools and skills: the separate observer-library repository) | `data/`: catalog, settings, proposals, log |
| | `runs/`: every agent run (steps, code, data queries, report, resume state); `library/`: house tools and skills, their history, imported packs, audit flags, test results |
| | `sandbox/venv`: the sandbox's Python environment |
| | `exports/`: database exports (`.parquet`) and uploaded imports |
| | InfluxDB: the Docker volume `observer-influx-dev` |

The data directory is `OBSERVER_HOME`, default
`~/.local/share/heatpumpsn-observer`; in Docker it will be a volume.
`observer init` creates it from the defaults and never overwrites existing
files.

## Architecture

```
MQTT (home broker; in development the mirror under mirror/)
   │  controller · EMS-ESP · MixerMeter · Zigbee2MQTT · HA Statestream
   ▼
 payload.py   messages → signals (id = topic or topic#json.path), values normalised
 metadata.py  HA discovery, Zigbee2MQTT device list, Statestream → signal metadata
 catalog.py   every signal ever seen (automatic): first/last seen, count, last value, metadata
 registry.py  your selection and descriptions (registry.yaml in the data directory): store or not, name,
              quantity, unit, location, circuit, role; rules for whole families
 store.py     raw InfluxDB writes: one point per sample, arrival time in ns, no aggregation
 llm.py       optional: proposes descriptions for undescribed signals (OpenAI-compatible)
   ▼
 later: processing (resampling at read time), anomaly rules → alerts over MQTT to HA,
        identification, parameter proposals and backtests, Docker
```

### Signals

* id: the topic for a scalar payload, `<topic>#<json.path>` for a JSON field:
  `heatpumpsn/controller/state/temp_mix`, `ems-esp/boiler_data#dhw.curtemp`,
  `ems-esp/temperaturesensor_data#28_FF64_1E9F_9836.temp`,
  `ha_state/sensor/bth01_6f31_temperature/state`.
* values: numbers as float; on/off, true/false, online/offline as 1/0;
  anything else as text; empty, `unknown` and `unavailable` are dropped.
* **Retained messages are catalogued but never stored**: they replay an old
  value whose time is unknown.

### Storage (InfluxDB 2.x)

The database holds recorded house data and is **not** disposable.
`scripts/influx-reset.sh --yes-delete-all-data` deletes the volume and
rebuilds it empty from `docker-compose.dev.yml`; without that flag it refuses.

Measurement `signal`; tags `id`, `source` (first topic level) and, from the
registry, `quantity`, `location`, `circuit`, `role`; field `value` (float) or
`text`; time = arrival time. Example: all bottom-floor temperatures are
`r.location == "BF" and r.quantity == "temperature"`.

### Registry

`registry.yaml` (in the data directory; the generic starting version is
`observer/defaults/registry.yaml`): `rules` give families defaults (all
`temperature_humidity_sensor_*_temperature` are room temperatures in °C);
`signals` describe single signals and add to their rule (a room needs only a
name and a floor). `store_unregistered: false` keeps unknown signals out of the
database; they still appear in the catalog. New sensors therefore show up in
`observer catalog --unregistered`, and either a rule already covers them or
you describe them, by hand or with `observer propose` + `observer accept`.

### LLM assistance (optional)

Provider and model come from the Settings page (see "LLM providers and
models"); for Ollama use `http://<host>:11434/v1` and no key.

`observer propose` sends undescribed signals (id, stream metadata, last value)
together with the house description from the config (`context`) and the
already described signals as examples to an OpenAI-compatible endpoint
(LiteLLM, a local model, a hosted API). The validated answers go to
`data/proposals.yaml` with a confidence; `observer accept <id>` or
`observer accept --all --min-confidence 0.8` copies them into the registry.
Nothing changes without that step, and everything works without an LLM.

## Web portal

`observer serve` runs the ingest and the portal in one process
(default `http://127.0.0.1:8090/`):

* **Status:** MQTT connection, stored samples, signals per source and how
  fresh each source is.
* **Signals:** the whole catalog with filters (stored, not stored,
  undescribed, by source, search). Click a signal to describe it and choose
  whether it is stored; the change is saved to `registry.yaml` and applies to
  the next message, without a restart. "Remove own entry" falls back to the
  family rule.
* **Proposals:** ask the LLM about undescribed signals, then accept, edit and
  accept, or reject each proposal.
* **Rules:** the family rules from `registry.yaml` (edited in the file).
* **House:** the reviewed house description, see below.
* **Settings:** LLM provider names and the model per task.

Signals can be filtered by source, unit (including "no unit"), quantity,
device and description state, and the shown rows selected for bulk changes
(store / don't store, location, circuit, role, quantity). In the edit dialog,
write a draft description in your own words and press **Polish with LLM**: the
model rewrites it using the signal's metadata, unit, rule and neighbouring
signals, and says what it changed; you review and save.

## House understanding

How the observer understands the house, kept in three parts (House tab):

1. **Your review** (`house/notes.md`): free text, any language, always
   editable: corrections, missing facts, answers to the questions, comfort
   targets, changes in the house.
2. **Owner knowledge** (`house/knowledge.md`): the facts from your reviews
   that the sensors cannot know (building, hydraulics and equipment, rooms and
   use, comfort targets and schedules, what a signal really measures,
   history, known issues), consolidated and formatted by the LLM. Newer
   statements override older ones; each draft lists what changed. It is
   authoritative for the understanding and the agent.
3. **House understanding** (`house/description.md` + `model.yaml`): the
   description for you (overview, boiler and DHW, each circuit, rooms,
   outdoor, controller and devices, how the parts interact, uncertain points)
   and the same in machine form (systems, points, relations, checks, gaps),
   built from the signals and the owner knowledge.

**Cycle:** *Generate a draft* merges your review into the owner knowledge,
then builds the understanding from it (two LLM requests) → read both, answer
the **questions** in your review, look at the **recommendations** → edit the
knowledge or the understanding in the draft if needed (*Rebuild the
understanding from the knowledge* uses edited knowledge without a new
consolidation) → *Accept and archive*: knowledge and understanding become the
accepted version and go to `house/archive/<time>/` together with the review
they came from and `review.yaml` (questions, recommendations, changes, model,
whether it was edited); optionally your review is cleared. The draft waits in
`data/house_draft.yaml`. Signal ids named in the text that do not exist are
flagged.

## LLM providers and models

`.env` holds only endpoints and keys, one slot per provider:

```
OBSERVER_LLM_BASE_URL=http://localhost:38080/v1     # slot 1
OBSERVER_LLM_KEY=
OBSERVER_LLM_2_BASE_URL=http://ollama:11434/v1      # slot 2 (up to 9)
OBSERVER_LLM_2_KEY=
```

The **Settings** page names each slot and assigns a provider and model to each
task: *describe* (polish one signal's description), *propose* (bulk
proposals), *house* (the house description). It can list the models a
provider offers. These choices are not secrets and live in
`data/settings.yaml` in the data directory. Transient provider errors (rate limits, overload) are
retried; an answer that is not valid JSON gets one repair round.

## Agent, sandbox, data socket and library

The agent answers questions about the house from the recorded data
(`plans/observer_agentic_plan.md`). Start it from the portal's **Agent** tab,
from the command line (`observer agent [--skill NAME] QUESTION`), or use its
core tools from any MCP client (`observer mcp`).

```
 model (Settings: task "agent")                       run workspace runs/<id>/
   │ function calls                                     run.yaml, steps.jsonl (audit)
   ▼                                                    queries.jsonl (every data request)
 core tools (inside the observer)                       state.json (resume point)
   house_context, find_signals, data_coverage,          report.md, *.png, data/, .runs/*.py
   signal_features, get_series, get_events ── InfluxDB, read-only token
   list_library, read_library_item,
   create_tool, save_skill ─────────────────── library (house part, unaudited)
   run_python ─► sandbox (bwrap, no network)
                   from obs import data, tools
                   data.series(...) ── Unix socket ──► data service ── InfluxDB (read-only)
                   tools.load(name).run(...) ◄── library tools, read-only mount
   finish ── report, findings, proposals
```

* **The agent chooses the data and the time windows.** Its code in the
  sandbox reads the database itself: `obs.data.series(ids, start, stop,
  every, agg)` returns a pandas DataFrame; also `find`, `coverage`, `events`,
  `house`. The sandbox has no network; the only way out is the run's **data
  socket** (`agent/broker.py`), a Unix socket file bound into the sandbox. The
  service behind it runs in the observer, checks every request (known signal
  ids, strict time and window formats, size limits), reads InfluxDB with the
  read-only token (never visible to the code) and logs every request in
  `queries.jsonl`.
* **Sandbox** (`agent/sandbox.py`): bubblewrap, rootless, all namespaces
  unshared, only `/usr`, its own Python (numpy, pandas, scipy, matplotlib,
  pytest), the `obs` library, the tool library (read-only), the workspace and
  the socket visible; cleared environment; memory and process limits by a
  systemd user scope, CPU time and file size by rlimits, wall-clock timeout.
* **Library** (`agent/library.py`, Library tab): analysis **tools** (a folder
  with `tool.yaml`: version, purpose, the kind of data it needs, typed
  inputs, output; `tool.py` with `run(...)`; `test_tool.py`: pytest on
  synthetic data, run in the sandbox) and **skills** (`SKILL.md`: version and
  a recipe, domain knowledge or a method that worked). The tools and skills
  themselves are **not in this repository**: they live in their own
  repository, **observer-library** (`~/Work/observer-library`, versioned,
  with a changelog and checks). Sources, highest precedence first:
  1. *house* items in `<OBSERVER_HOME>/library/`, written by agents
     (`create_tool`, `save_skill`), the harvest or by hand; saving one again
     raises its version and keeps the previous one in `library/history/`;
  2. library folders from `agent.library_paths` in config.yaml (e.g. the
     observer-library checkout; edits are live);
  3. imported **packs** in `<OBSERVER_HOME>/library/packs/`.
  **Export/import**: a `.tgz` with `library.yaml` (pack name, version, every
  item's version and content hash) and `tools/`, `skills/` (portal Library
  tab or `observer library export|import`). Imports are checked strictly
  (only `.py/.yaml/.md` under `tools|skills/<name>/`, no links, no `..`,
  size limits); a newer version of a pack replaces the older one, which is
  kept. Tools are generic (series, frames, numbers; no signal ids), so they
  can move to another house. **Audit** is the owner's flag, bound to the
  content hash: any change voids it, identical content stays audited after an
  export and re-import. The agent sees the flags (an index is in its prompt,
  loading an unaudited tool prints a warning) and the run records which items
  it used and whether they were audited.
* **Harvest**: the button *Find tools and skills in this run* sends the run
  (code, outputs, report) and the library index to the task *harvest*
  (1-2 requests; a strong model pays off) and lists proposed tools and skills;
  *Add to library* saves one (unaudited, a tool's tests run at once).
* **Pause and resume**: the conversation is saved after every step. LLM
  errors are handled by kind:
  * 429 **with** a stated delay (Retry-After header, Google's `retryDelay`,
    or "retry in …" in the message): up to a minute is waited out and the
    request repeated (up to 5 times); a longer delay pauses the run at once;
  * 429 **without** time information: if the message says the quota is used
    up ("per day", "billing", "limit: 0") the run pauses at once; otherwise
    (busy, overloaded, too many requests) short waits of 5, 10, 20, 30 s and
    repeats, then the run pauses; the same for 5xx errors and an unreachable
    endpoint.
  A paused run is not failed; the run view says why and, when known, when
  the provider expects the quota back.
  *Resume* continues from the saved step with the model now set for the task
  *agent*, so you can wait or switch the model.
* **Runs**: one at a time, at most 25 model steps and 30 minutes.

Setup once: the read-only token (below), `observer sandbox-setup`, models for
the tasks *agent* and *harvest* on the Settings page.

```bash
docker exec observer-influx-dev influx auth create --org heatpumpsn \
  --read-bucket <bucket id from `influx bucket list`> --description "observer agent tools: read-only" -t <admin token>
# put the token into .env as OBSERVER_INFLUX_READ_TOKEN
.venv/bin/observer sandbox-setup
.venv/bin/observer library                  # sources, tools and skills with versions, audit and test state
.venv/bin/observer library export -o house.tgz          # house items as a pack (--all, --item tools/NAME)
.venv/bin/observer library import observer-library-0.2.0.tgz
.venv/bin/observer test-tool fopdt_fit      # a tool's tests in the sandbox
.venv/bin/observer agent --skill sensor_health
.venv/bin/observer agent "Why did the floor circuit heat slowly this morning?"
uv pip install -e ".[mcp]" && .venv/bin/observer mcp     # stdio MCP server
```

## Deployment (release)

Releases are tags `observer-v<version>` with a deploy bundle
`heatpumpsn-observer-<version>.tar.gz` (`scripts/release.sh`, the GitHub
workflow `.github/workflows/observer-release.yml`, `CHANGELOG.md`).

`deploy/docker-compose.yml` runs InfluxDB and the observer (image from
`Dockerfile`); settings in `deploy/.env` (`deploy/env.example`). Step by step,
including moving the state and the history from the development observer:
[`deploy/README.md`](deploy/README.md). The release configuration takes the
machine-specific settings from the environment (`OBSERVER_MQTT_HOST`,
`_PORT`, `_USER`, `_PASSWORD`, `_TOPIC_PREFIX`, `OBSERVER_INFLUX_URL`,
`_ORG`, `_BUCKET`); `observer influx-setup` creates the read-only token; a
portal password (`OBSERVER_PORTAL_PASSWORD`, HTTP Basic) protects everything
but `/healthz`. `observer state export|import` moves registry, catalog, house
understanding, house library, settings and context between observers.

## Database export and import

The recorded samples can be taken out of InfluxDB and brought back as **one
compact file**: Parquet with zstd (about 2 bytes per sample: the first day,
about 1 million samples, is 2.0 MB; InfluxDB itself uses 20 MB). One row per
sample, lossless (`time_ns` in nanoseconds, `id`, `field`, `value` / `text`,
the tags), sorted by signal and time; the file's metadata carries the export
time, range, counts and the registry's description of every signal. DuckDB
reads it directly:

```sql
SELECT make_timestamp(time_ns // 1000) AS t, value
FROM 'observer-20261102-153653.parquet' WHERE id = 'heatpumpsn/controller/state/temp_mix';
```

* **Export** (Status tab, or `observer db export [--start T] [--stop T] [-o FILE]`)
  reads with the read-only token, day by day, and writes the file into
  `<OBSERVER_HOME>/exports/`.
* **Import** (Status tab, or `observer db import FILE [--dry-run]`) **never
  deletes or overwrites**. A sample counts as present when the same signal
  and field have a sample at the same timestamp (tags are not compared: they
  change when the registry changes); for every signal the import reads the
  timestamps InfluxDB already has in the file's range and writes only the
  missing samples. Importing a file twice, or a file that overlaps the
  database, therefore adds nothing twice. A dry run only counts. Uploaded
  files are kept in `exports/uploads/`.

Checked on the real data: re-importing a full export wrote 0 of 1,003,897
samples; into an empty bucket the first import wrote everything, the second
nothing, and a re-export was identical row for row.

## Development

```bash
cd observer
uv venv && uv pip install -e ".[dev]"
.venv/bin/observer init                     # creates ~/.local/share/heatpumpsn-observer (or $OBSERVER_HOME)
# edit config.yaml and .env there
.venv/bin/python -m pytest

.venv/bin/observer serve --dry-run          # portal on http://127.0.0.1:8090/, nothing written
.venv/bin/observer listen --seconds 120     # build the catalog, store nothing
.venv/bin/observer catalog                  # inventory: STORE / - , name, last value, rule
.venv/bin/observer catalog --unregistered   # what is not described yet
.venv/bin/observer serve --dry-run            # full ingest without a database

# with InfluxDB (holds recorded data: reset only with the owner's agreement)
# scripts/influx-reset.sh --yes-delete-all-data   # DELETES all recorded data, then recreates org, bucket, token
.venv/bin/observer serve
```

In development the data comes from the home broker's bridge (topics under
`mirror/`, config `topic_prefix`). At home the observer subscribes to the
broker directly: `topic_prefix: ""`, `subscribe: ["#"]`.

## Data sources

| Source | Topics | Content |
| --- | --- | --- |
| Controller | `heatpumpsn/controller/...` | temperatures, pumps, heat call, mixer position, setpoint, errors, power, MPC status, logs |
| EMS-ESP | `ems-esp/boiler_data`, `temperaturesensor_data`, `heartbeat`, `status` | boiler flow/setpoint, burner, starts, DHW; circuit forward/return (DS18B20); health |
| MixerMeter | `mixermeter/sensor/<x>/state` | independent mixer position (encoder), angle |
| Zigbee2MQTT | `zigbee2mqtt/<device>` | lights, switches, plugs, motion/radar (temperature, humidity), door and flood sensors |
| Home Assistant | `ha_state/<domain>/<entity>/state` (MQTT Statestream) | room temperatures and humidity (BLE sensors) |

## Roadmap

1. **Ingest and storage** (this step): catalog, registry, raw InfluxDB storage,
   web portal for status, signals and proposals.
2. **Agent, tools, sandbox, skills** (done, first version): see above.
3. **Anomalies and alerts:** rules on the stored data (staleness, stuck values,
   plausibility between redundant sensors, short-cycling, heat not delivered),
   alerts as HA entities over MQTT.
4. **Identification and optimisation:** transport delay, valve gain and
   coupling from the data; parameter proposals backtested against the recorded
   data and the simulator plant model (`floor_simulator/`).
5. **Docker:** the observer and InfluxDB as one compose stack at home.

The portal grows with each step: charts of the stored data, the alert list
and its rules, identification results and parameter proposals with their
backtests.
