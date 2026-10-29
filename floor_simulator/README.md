# Floor-heating HIL simulator & web bench

Implements `specification.md` (v2). A Python service closes the loop around an
ESP32 controller flashed with the emulation firmware (`esp32dev_sim`,
`-DHPN_IO_EMULATION`):

```
 ESP32 controller ──/api/state, /api/sim (1 s)──▶ telemetry ─▶ relays, valve position
        ▲                                                      │
        │ /api/sim/temperature (slew ≤ 0.15 K/s)               ▼
 injector ◀── plant (Step 6 coupled model, 10 s) ◀── boiler / DHW emulator
        HA role: MQTT cmd/* demands + 300 s keep-alive + setpoint
```

## Run

From the repository root (`HeatpumpsN/`):

```bash
uv run --project floor_simulator python -m floor_simulator.hil_server \
    --controller-ip localhost:30080 --mqtt-broker localhost:31883 [--port 8080]
# offline, no ESP32 or broker:
uv run --project floor_simulator python -m floor_simulator.hil_server --mock
```

Open `http://<host>:8080/`. Options: `--mqtt-user/--mqtt-password`, `--mqtt-base`
(default: read from the controller), `--preset`, `--max-rate`, `--seed`,
`--expect-tuning-hash`, `--no-safe-shutdown`, `--log-dir` (default `floor_simulator/logs`).

Startup preflight fails unless `/api/status` reports `portal.emulation` and `/api/sim`
answers; it also sets `/api/sim/mode` to follow the controller with the sine boiler off.
On Ctrl-C / SIGTERM all demands and DHW are switched off and logs are flushed.

## Layout

| Path | Role |
|---|---|
| `hil_server.py` | CLI entry point |
| `app/plant.py` | Stateful port of `log_analysis/step6/plant_v1.py` (coupled mode), bit-exact |
| `app/boiler.py` | Boiler modes (observed cycling / static / off) and DHW |
| `app/controller.py` | REST client, telemetry parsing, preflight, slew-limited injector |
| `app/ha_mqtt.py` | Home Assistant role: demands, keep-alive, setpoint, discovery |
| `app/engine.py` | Loops (1 s telemetry, 10 s physics, 30 s counters), events, wear metrics, presets |
| `app/api.py` | FastAPI: dashboard, `/api/control/*`, `/api/history`, `/ws` |
| `app/mock_controller.py` | Offline ESP32 contract double (`--mock`, tests) |
| `app/static/` | Dashboard (uPlot vendored, no CDN at runtime) |
| `tests/` | Plant parity vs reference, API / injector / mock closed-loop tests |

## Logs

`logs/<date-time>-<id>/`: `session.json` (UUID, git commit, SHA-256 of
`plant_parameters.json`, firmware and tuning hash), `hil_timeseries_YYYYMMDD.jsonl`
(10 s) and `mixer_actuation_events_YYYYMMDD.jsonl`. Query with DuckDB:
`SELECT * FROM read_json_auto('floor_simulator/logs/*/hil_timeseries_*.jsonl')`.

## Notes and deviations from the spec

* **Telemetry is REST polling only.** §2 forbids the controller WebSocket for external
  clients, so the §7.4 / step-3 WebSocket listener is not implemented. `/ws` here is
  the simulator's own feed to its dashboard.
* **Valve position for the plant** is the emulated carrier's physical position
  (`/api/sim` `mixer.position`). The controller's dead-reckoned estimate is only used if
  that is missing. Relays come from the carrier's effective outputs.
* **Pump power** uses the firmware's own `realisticPower` emulation; the simulator does not
  call `/api/sim/power`.
* **DHW** is signalled to the controller via `/api/sim/intent {dhw_active}` and the virtual
  `binary_sensor` (`floor_sim/binary_sensor/dhw_charging/state`). Discovery configs are
  published retained under `homeassistant/{sensor,binary_sensor}/floor_sim/...`. The
  controller binds them only when its `mixer_meter_device=floor_sim`,
  `mixer_meter_field=mixer_position` / `dhw_meter_device=floor_sim`,
  `dhw_meter_entity=dhw_charging` are set (left unchanged here).
* **Without `--mqtt-broker`** demands go through `/api/sim/intent`; the firmware has no
  REST setpoint route, so the setpoint cannot be changed in that mode.
* **Boiler emulator constants not identified from data** (approach time constants, pause
  between firing cycles, DHW burner %) are named at the top of `app/boiler.py`.
* Python ≥ 3.10 (the existing `.venv` is 3.10; the spec asks for 3.11+, but nothing here needs it).

## Tests

```bash
cd floor_simulator && .venv/bin/python -m pytest
```
