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

## Architecture

```
MQTT (home broker; in development the mirror under mirror/)
   │  controller · EMS-ESP · MixerMeter · Zigbee2MQTT · HA Statestream
   ▼
 payload.py   messages → signals (id = topic or topic#json.path), values normalised
 metadata.py  HA discovery, Zigbee2MQTT device list, Statestream → signal metadata
 catalog.py   every signal ever seen (automatic): first/last seen, count, last value, metadata
 registry.py  your selection and descriptions (registry.yaml): store or not, name,
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

Measurement `signal`; tags `id`, `source` (first topic level) and, from the
registry, `quantity`, `location`, `circuit`, `role`; field `value` (float) or
`text`; time = arrival time. Example: all bottom-floor temperatures are
`r.location == "BF" and r.quantity == "temperature"`.

### Registry

`registry.yaml` (in git): `rules` give families defaults (all
`temperature_humidity_sensor_*_temperature` are room temperatures in °C);
`signals` describe single signals and add to their rule (a room needs only a
name and a floor). `store_unregistered: false` keeps unknown signals out of the
database; they still appear in the catalog. New sensors therefore show up in
`observer catalog --unregistered`, and either a rule already covers them or
you describe them, by hand or with `observer propose` + `observer accept`.

### LLM assistance (optional)

`observer propose` sends undescribed signals (id, stream metadata, last value)
together with the house description from the config (`context`) and the
already described signals as examples to an OpenAI-compatible endpoint
(LiteLLM, a local model, a hosted API). The validated answers go to
`data/proposals.yaml` with a confidence; `observer accept <id>` or
`observer accept --all --min-confidence 0.8` copies them into the registry.
Nothing changes without that step, and everything works without an LLM.

## Development

```bash
cd observer
uv venv && uv pip install -e ".[dev]"
cp config.example.yaml config.yaml          # adjust; config.yaml is git-ignored
.venv/bin/python -m pytest

.venv/bin/observer listen --seconds 120     # build the catalog, store nothing
.venv/bin/observer catalog                  # inventory: STORE / - , name, last value, rule
.venv/bin/observer catalog --unregistered   # what is not described yet
.venv/bin/observer run --dry-run            # full ingest without a database

# with InfluxDB (docker-compose.dev.yml)
docker compose -f docker-compose.dev.yml up -d
export INFLUX_TOKEN=observer-dev-token
.venv/bin/observer run
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

1. **Ingest and storage** (this step): catalog, registry, raw InfluxDB storage.
2. **Anomalies and alerts:** rules on the stored data (staleness, stuck values,
   plausibility between redundant sensors, short-cycling, heat not delivered),
   alerts as HA entities over MQTT.
3. **Identification and optimisation:** transport delay, valve gain and
   coupling from the data; parameter proposals backtested against the recorded
   data and the simulator plant model (`floor_simulator/`).
4. **Docker:** the observer and InfluxDB as one compose stack at home.
