# HeatpumpsN observer — handover

Entry point for continuing the observer in a new session. Updated 2026-11-01.
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
* **Running:** `observer serve` (portal `http://127.0.0.1:8090/`, no login yet),
  started with `setsid nohup ~/Work/HeatpumpsN/observer/.venv/bin/observer serve
  > ~/.local/share/heatpumpsn-observer/data/serve.log 2>&1 &`. Stop it with
  `kill $(pgrep -x observer)` (not `pkill -f`, which matches its own shell).
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
  flash-lite now (the owner's choice, 2026-11-02).
* **House understanding** (three parts: your review, owner knowledge,
  understanding): a draft from before the knowledge step is waiting; the
  owner's review (notes.md) has content that "Generate a draft" will
  consolidate. Nothing accepted yet.
* **Next (plan phases):** C deterministic supervision from the accepted model's
  checks with alerts to HA (first rule: EMS-ESP offline / heartbeat missing);
  then the tool layer, sandbox and agents (`plans/observer_agentic_plan.md`).
  Copy the old HA InfluxDB year in once (read-only token) before HA's InfluxDB
  integration is removed.


### 2.1 Agent, tools, sandbox, skills (first version, 2026-11-01)

* Portal **Agent** tab, `observer agent [--skill NAME] [QUESTION]`, and
  `observer mcp` (the same tools for MCP clients). Design and limits in
  `README.md` → "Agent, tools, sandbox and skills".
* Tools run inside the observer and read the database with the **read-only
  token** `OBSERVER_INFLUX_READ_TOKEN` (in the data directory's `.env`;
  created 2026-11-01, a write with it returns 403). Generated code runs in
  **bubblewrap** (rootless, no network: data reaches it only as CSV files the
  tools wrote; cgroup memory/process limits via `systemd-run --user`; CPU
  time by rlimit, since the cpu controller is not delegated to the user).
  The sandbox's Python lives in `<OBSERVER_HOME>/sandbox/venv`
  (`observer sandbox-setup`).
* Skills: `sensor-health`, `heating-start`, `boiler-cycling` in
  `observer/skills/`. Runs and their audit records in `<OBSERVER_HOME>/runs/`.
* Model for the task *agent*: `gemini-3.5-flash-lite` (runs take 30–80 s,
  ~140k tokens). The first runs found real things (overshoot at heating
  starts, MPC anomalies, BLE sensor gaps) but also misread facts the
  unreviewed house description does not settle (see ISSUES).
* Docker is available here too, but the user is in the `docker` group
  (root-equivalent), so it is not used as the default sandbox.

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
