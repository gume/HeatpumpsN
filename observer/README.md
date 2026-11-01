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
| `docker-compose.dev.yml`, scripts | `house/`: description, model, review notes, archive |
| | `data/`: catalog, settings, proposals, log |
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

## House description (reviewed)

The observer's understanding of the house, written for the owner to check and
extend. Files in `house/` in the data directory:

* `description.md`: the audited description (overview, boiler and DHW, each
  circuit, rooms, outdoor, controller and devices, how the parts interact,
  uncertain points);
* `model.yaml`: the same understanding in machine form (systems, points,
  relations, checks, gaps), read by analysis and anomaly checks;
* `notes.md`: the owner's review notes: corrections, missing facts, answers to
  the questions. Always editable; every regeneration uses them;
* `archive/<time>/`: every audited version with its notes, model and review
  data (questions, recommendations, which model wrote it, whether it was edited).

**Cycle:** *Generate a proposal* (from the house context, all described
signals, the not-yet-used catalog signals, the notes and the previous audited
description) → read it, answer its **questions** in the notes, look at the
**recommendations** (existing signals worth using, sensors worth adding) →
edit the text if needed or regenerate → *Accept and archive*. Signal ids named
in the text that do not exist are flagged above the proposal. The proposal
waits in `data/house_proposal.yaml` until it is accepted.

The quality depends on the model: the house description benefits from a
strong reasoning model (Settings).

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

# with InfluxDB: disposable, rebuilt empty at any time
scripts/influx-reset.sh --yes-delete-all-data   # DELETES all recorded data, then recreates org, bucket, token
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
2. **Anomalies and alerts:** rules on the stored data (staleness, stuck values,
   plausibility between redundant sensors, short-cycling, heat not delivered),
   alerts as HA entities over MQTT.
3. **Identification and optimisation:** transport delay, valve gain and
   coupling from the data; parameter proposals backtested against the recorded
   data and the simulator plant model (`floor_simulator/`).
4. **Docker:** the observer and InfluxDB as one compose stack at home.

The portal grows with each step: charts of the stored data, the alert list
and its rules, identification results and parameter proposals with their
backtests.
