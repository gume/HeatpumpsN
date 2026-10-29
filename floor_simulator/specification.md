# System Specification v2: ESP32 Floor-Heating HIL Simulator & Web Bench

**Status:** Version 2 (Supersedes v1).  
**Verification Target:** Aligned against repository HEAD (`e872c9e`), firmware source code (`firmware/`), analysis reports (`analysis/`), parameter extraction logs (`floor_simulator/log_analysis/`), and planning documents (`plans/`).  
**Purpose:** Provides complete, verified architectural, functional, and engineering specifications for the autonomous coding agent and developer building the ESP32 Hardware-in-the-Loop (HIL) floor-heating simulator and web testbench.

---

## 0. Document Control & v1 → v2 Changelog

Version 1 of the specification was drafted before the controller firmware source code and detailed sub-plans were fully accessible in the repository. Version 2 corrects assumptions, integrates actual firmware contracts, resolves hardware/software omissions, and adds rigorous best-practice recommendations.

| v1 Assumption / Claim | v2 Corrected Reality | Source / Evidence |
| :--- | :--- | :--- |
| **Generic REST Temperature Injection** | Temperature injection is supported **only** via emulation-specific REST endpoints (`/api/sim/*`) available exclusively in `-DHPN_IO_EMULATION` firmware builds (`esp32dev_sim`). | `firmware/src/web_portal.cpp` (§5) |
| **5 Temperature Channels (incl. `T_incoming`, `T_forward`)** | The controller measures and manages exactly **3 temperature channels** (`TempRole::Boiler`, `ReturnFloor`, `MixOut`). The controller's "Boiler" sensor is physically mounted on the HX secondary outlet (`T_incoming`). | `firmware/include/hpn/io_types.h`, `config.cpp` |
| **PID Controller & Telemetry** | Control is implemented as **Proportional + Feedforward + Integrator** (PI + FF), not standard PID. No `p`/`i`/`d` terms are published in MQTT/REST; only `mixerIntegrator` and configurable `kp`/`ki`. | `firmware/src/l2_control.cpp`, `web_portal.cpp` |
| **Mixer State % over MQTT** | MQTT topic `state/mixer_state` publishes enum strings (`"idle"`, `"opening"`, `"closing"`), **not percentages**. Position percentages live in `/api/state` (`circuits.floor.mixerPos`). | `firmware/src/mqtt_client.cpp`, `web_portal.cpp` |
| **Periodic Zero Recalibration** | While automatic periodic re-homing is planned for future firmware, initial zeroing is executed at controller boot. | `firmware/src/l1_safety.cpp` |
| **Control Parameter Tuning** | Control parameters are managed within the controller (using built-in presets/defaults); no external observer node is active in this HIL test setup. | `firmware/src/config.cpp` |
| **Repository Path** | Absolute root paths are stripped; the repository head is located at the project root (`HeatpumpsN`), which can be placed anywhere in the filesystem. | Environment metadata |

---

## 1. Repository Layout & Discovery Phase

Before implementing or running the simulator, verify the local file tree:

1. **`plans/`**: Controller and system design specifications.
   * `plans/master_plan.md`: System actors and master topology.
   * `plans/controller node/01_config_and_contracts.md`: `struct Config`, `SystemState`, and divergence reason codes.
   * `plans/controller node/06_interfaces_and_logging.md`: MQTT discovery schemas, command topics, and JSON payload contracts.
2. **`firmware/`**: ESP32 controller firmware.
   * `firmware/include/hpn/config.h`: Compile-time and NVS parameter definitions.
   * `firmware/include/hpn/state.h`: Runtime state machine contracts.
   * `firmware/src/sim.cpp`: In-memory emulation carrier model (active under `HPN_IO_EMULATION`).
   * `firmware/src/web_portal.cpp`: HTTP routes, REST endpoints, WebSocket streaming, and simulation handlers.
3. **`floor_simulator/log_analysis/`**: Canonical parameter database.
   * `plant_parameters.json`: Assembled parameter set from Steps 2 through 6.
   * `step6/plant_v1.py` & `plant_v1.hpp`: Reference coupled 10 s plant engines.

---

## 2. System Architecture & Runtime Requirements

* **Execution Model:** A headless Python background service (`floor_simulator/hil_server.py`) running continuously alongside an ESP32 controller flashed with emulation firmware (`-DHPN_IO_EMULATION`), ensuring all emulation REST endpoints (`/api/sim/*`) are fully available.
* **Stack:** Python `>=3.11`, managed via `uv`, using `FastAPI`, `uvicorn`, `asyncio`, `httpx`, `paho-mqtt` / `aiomqtt`, and `duckdb`.
* **CLI Entrypoint:**
  ```bash
  uv run python -m floor_simulator.hil_server \
    --controller-ip <ESP32_IP> \
    --mqtt-broker <BROKER_HOST:PORT> \
    [--mqtt-user <USER>] \
    [--mqtt-password <PASS>] \
    [--port 8080] \
    [--log-dir ./floor_simulator/logs]
  ```
* **Transport Layers:**
  * **Controller $\rightarrow$ Simulator State Feed:** Standard HTTP REST polling of `/api/state` at a `1.0 s` cadence (avoiding WebSockets, which are intended for internal UI use only and may be restricted/removed from external clients).
  * **Simulator $\rightarrow$ Controller Sensor Injection:** POST requests to `/api/sim/temperature`, `/api/sim/power`, and `/api/sim/intent` (emulation endpoints available via emulation firmware).
  * **Simulator $\rightarrow$ Home Assistant Broker:** Standard MQTT client publishing heating demands, setpoints, and discovery configs.

---

## 3. Physics, Emulation & Control Loops

### 3.1 Fast Telemetry & Actuation Monitoring Loop (`1.0 s` interval)
1. Polls the controller's `/api/state` REST endpoint every `1.0 s`.
2. Monitors physical relay states (`pump_floor`, `pump_rad1`, `pump_rad2`, `heater`), mixer motion state (`mix_motion`, `mix_dir`), dead-reckoned position (`pos_est_dr`), and system alarms.
3. Detects relay and mixer actuation events to populate the event log.

### 3.2 Physics & Temperature Injection Loop (`10.0 s` interval, $\Delta t = 10.0\text{ s}$)
Advances the coupled physical model (`plant_v1.py` logic) and pushes emulated water temperatures to the ESP32 emulation REST interface.

1. **Boiler Outgoing Water (`T_boiler`) & HX Model (`step2_hx`):**
   * Computes primary boiler temperature (`T_boiler`) and passes it through the Step 2 Heat Exchanger model (`hx_model_v71.py`) to produce `T_inc,water` and sensor temperature `T_incoming` (with thermowell lag $\tau_s = 75.27\text{ s}$).
   * **Three Boiler Modes:**
     * **Mode 1 — Observed Realistic Operation (Default):** Driven by empirical distributions (`plant_parameters.json`): steady CH modulation (`bp_ch_steady_median = 23%`), firing band ($P_{10} = 62.9\ {\circ}\text{C}$, median $= 68.7\ {\circ}\text{C}$, $P_{90} = 70.3\ {\circ}\text{C}$), and firing cycles (median duration $970\text{ s}$). When idle, cools via Step 2 time constants ($\tau_{\text{fn}} = 2123.6\text{ s}$, $\tau_{\text{rc}} = 1421.6\text{ s}$).
     * **Mode 2 — Static Temperature:** Fixed `T_boiler` ($20\ {\circ}\text{C} - 85\ {\circ}\text{C}$, default $68.7\ {\circ}\text{C}$) while burner is ON.
     * **Mode 3 — Switched OFF:** Burner forced to $0%$; temperatures decay toward ambient.

2. **DHW Operation:**
   * Manual ON/OFF or timed duration (observed median $1170\text{ s}$, $P_{90} = 1626\text{ s}$).
   * When DHW is active, the boiler diverter removes heating flow (`DIV_CLEAR_S = 30.0\text{ s}`, `dhw_hold_s = 22.68\text{ s}`) while `T_boiler` rises toward `tb_dhw_max_median = 81.5\ {\circ}\text{C}`.

3. **3-Way Mixer Model (`step3` & `step4_mixer_dynamics`):**
   * Computes mixed water temperature:
     $$T_{\text{mix}} = T_{\text{ret}} + x(\text{pos}) \cdot m_{\text{rad}} \cdot (T_{\text{inc,water}} - T_{\text{ret}})$$
   * Applies radiator pressure multiplier $m_{\text{rad}} = 0.7928$ when `P1` or `P2` runs ($1.0$ when floor-only).
   * Applies $40.0\text{ s}$ transport delay and $34.64\text{ s}$ forward sensor lag ($\tau_{\text{fwd\_sensor\_s}}$).
   * **Gradient-Safe Injection Constraint (Critical):** To prevent triggering L1 safety gradient cutoffs ($dT/dt > \text{grad\_limit} = 0.3\text{ K/s}$), temperature updates injected into the ESP32 must be slew-limited or interpolated between 10 s model ticks (maximum step $\le 0.2\text{ K/s}$).

4. **Underfloor Loop, Slab & Room Model (`step5_floor_slab`):**
   * Evolves `T_return`, latent slab temperature `T_slab`, and `T_room` using the 1-node model ($\tau_r = 305.04\text{ s}$, $\beta = 0.1872$, $\tau_{\text{in}} = 8308.3\text{ s}$, $\tau_{\text{out}} = 8412.05\text{ s}$, $\tau_{\text{off}} = 5040.76\text{ s}$).

5. **REST Temperature Injection:**
   * Pushes calculated temperatures to the ESP32 emulation endpoints (`/api/sim/temperature`) mapped to the controller's three temperature roles:
     * `boiler` role $\leftarrow$ `T_incoming` (sensor output).
     * `return` role $\leftarrow$ `T_return_floor`.
     * `mix` role $\leftarrow$ `T_mix_out` (forward flow).

---

## 4. Home Assistant MQTT Heating Demand & Setpoint Manager

The simulator acts as Home Assistant on the MQTT broker (`--mqtt-broker`):

1. **Circuit Heating Demands:** Publishes ON/OFF commands to `<base>/cmd/heating_loop_1`, `<base>/cmd/heating_loop_2`, and `<base>/cmd/floor_heating`.
2. **Automatic Keep-Alive Service:** Background task re-publishes active heating requests every $300\text{ s}$ (well within the controller's `demand_timeout_s = 600\text{ s}$). Includes a UI toggle ("Pause Keep-Alive") to verify controller failsafe timeout behavior (error `301`, demand drop, and post-after-run pump stop).
3. **Target Forward Setpoint:** Publishes floating-point setpoint values (e.g., `35.0`) to `<base>/cmd/floor_setpoint` (`number` entity, range 5.0–80.0 °C).
4. **Dynamic Sensor Provisioning:** Publishes Home Assistant discovery configurations for virtual sensors (such as mixer position encoder and DHW charging binary sensor) so the controller binds automatically without NVS hardcoding.

---

## 5. Web Interface Specification

Served statically by FastAPI, the dashboard provides:

1. **Control Cards:** Heating circuit toggles, target forward setpoint slider, boiler mode selector (`Observed Cycling` | `Static` | `OFF`), DHW duration selector, outdoor temperature diurnal curve generator, and State Presets ("Cold Slab Start", "Steady Winter", "Hot Slab", "Post-DHW Hot Slug").
2. **Live Charts:** Multi-pane time-series plots (uPlot or Chart.js) for temperatures, mixer position, relay states, and power consumption.
3. **Mixer Actuation & Wear Telemetry Panel:** Total valve travel ($\sum |\Delta\text{pos}|\%$), travel rate (%/hour), relay cycle counts (from `/api/counters`), reversal frequency, and rolling `T_forward` tracking RMSE against setpoint.

---

## 6. Telemetry & Mixer Actuation Logging

Persisted to disk (`./floor_simulator/logs/`) via DuckDB or JSONL rotation:

1. **`hil_timeseries` (`10.0 s` cadence):** `timestamp`, `t_boiler_primary`, `t_inc_water`, `t_inc_sensor`, `t_fwd_mix`, `t_fwd_sensor`, `t_fwd_setpoint`, `t_ret`, `t_slab`, `t_room`, `t_out`, `p1`, `p2`, `p3`, `dhw_active`, `burner_pct`, `mixer_state_pct`, `mixer_integrator`, `system_error_code`.
2. **`mixer_actuation_events` (Event log):** `timestamp_start`, `timestamp_end`, `duration_s`, `direction`, `pos_before_pct`, `pos_after_pct`, `delta_pos_pct`, `t_fwd`, `setpoint`, `p1_active`, `p2_active`.

---

## 7. Best-Practice Recommendations & Suggestions

To ensure robust, production-grade operation of the HIL simulator and avoid edge-case failures, implement the following recommendations:

1. **(P0) Slew-Limited Temperature Injection:** Implement a smoothing filter on the temperature injection client (`httpx`) to limit step changes to $\le 0.15\text{ K/s}$. This prevents false-positive L1 gradient cutoff trips (`WARN_GRADIENT_CUTOFF`) when transitioning between 10 s simulation ticks.
2. **(P1) Controller Firmware Build Validation:** Enforce an automated pre-flight check in `hil_server.py` that queries `/api/status` or `/api/sim` to verify `-DHPN_IO_EMULATION` is active before attempting temperature injections. Fail immediately with clear instructions if running against a bare hardware build.
3. **(P2) Deterministic Run Metadata & Hashing:** Tag every `hil_timeseries` log session with a unique UUID, git commit hash, and a SHA-256 hash of `plant_parameters.json` to guarantee full reproducibility of simulation runs.
4. **(P2) WebSocket Connection Watchdog:** Implement an automatic reconnection and state resync routine for the controller's WebSocket (`/ws`) feed. If telemetry stalls for $>2.0\text{ s}$, log a warning and fall back to polling `/api/state` until the WebSocket recovers.
5. **(P2) Automated Contract Testing (Mock Mode):** Before connecting to a live ESP32, provide a local mock ESP32 server mode (`--mock`) in the Python test suite to validate FastAPI routes, WebSocket parsing, and MQTT publishing logic entirely offline.
6. **(P3) Parameter Version Guard:** Have the simulator read the parameter hashes from `/api/config/export` at startup to ensure the ESP32 controller is running the expected control loop tuning parameters.
7. **(P3) Graceful Shutdown & State Persistence:** On SIGINT/SIGTERM, flush pending DuckDB/JSONL log buffers to disk and issue a safe shutdown command to the simulation environment without stranding relays in an active state.

---

## 8. Implementation Plan for the Coding Agent

1. **Step 1 (Audit & Scaffolding):** Verify dependencies in `.venv`, inspect `plant_parameters.json`, and set up `floor_simulator/app/` structure.
2. **Step 2 (Physics Port):** Port `step6/plant_v1.py` into a stateful `PlantEngine` class supporting the three boiler modes, DHW transients, radiator multiplier, and presets. Verify parity against reference tests.
3. **Step 3 (Bridges):** Implement the WebSocket telemetry listener (`250 ms`), REST temperature injector (`10.0 s` with slew limiting), and Home Assistant MQTT publisher with background keep-alive.
4. **Step 4 (Server & Dashboard):** Build the FastAPI backend, WebSocket forwarder to the UI, and the real-time uPlot dashboard.
5. **Step 5 (Logging & Verification):** Run end-to-end verification using the mock server and live ESP32 remote runner (`pio remote run --target upload`).
