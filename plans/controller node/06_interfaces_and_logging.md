# 06 — Logging, System Error Codes & External Interfaces (L4)

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (`struct Config`, `SystemState`, `DivergenceReason`), `03_safety_layer_l1.md`, `04_control_and_coordination_l2.md`, `05_modes_and_testing.md`  
**Outputs to:** External MQTT Broker (Home Assistant & Observer), Local HTTP Web Dashboard

---

## 1. Logging & System Error Codes

### 1.1 Standardized System Error Codes (`system_error_code`)

* The controller publishes a standardized `system_error_code` integer sensor to Home Assistant, grouped by severity and subsystem function:
  * `0` (`ERR_OK`): Correct, fault-free normal operation.
  * `101` (`ERR_L1_HARD_OVERTEMP`): L1 hard floor over-temperature cutoff triggered ($T_{\text{mix\_out}} \ge t_{\text{max\_hard}}$).
  * `201` (`ERR_SENSOR_MIX_LOST`): Floor mixed output temperature sensor (`T_mix_out`) persistent fault.
  * `202` (`ERR_SENSOR_RET_LOST`): Floor return temperature sensor (`T_return_floor`) persistent fault.
  * `203` (`ERR_SENSOR_BOILER_LOST`): Boiler supply temperature sensor (`T_boiler`) persistent fault.
  * `301` (`ERR_HA_KEEPALIVE_TIMEOUT`): a heating-loop keep-alive was missed beyond `demand_timeout_s`; the demand is stale until the next ON.
  * `401` (`ERR_RELAY_FAULT_WELDED`): Welded relay contact detected by pump power sensor.
  * `501` (`ERR_NVS_DATA_CORRUPTED`): Configuration CRC check failed in NVS; compiled-in safety defaults loaded.
  * `603` (`ERR_SENSOR_ROLE_SUSPECT`): Sensor-role plausibility check failed while heating.
  * `604` (`ERR_DHW_INFER_TIMEOUT`): Inferred DHW charging exceeded `dhw_infer_max_s`.
* The NVS warning/error is latched for the current boot. Saving settings rewrites the config blob but does not clear the live flag; a reboot clears it once every persisted blob validates. This keeps counter or anti-seize corruption visible instead of masking it behind a config save.
* A companion **`system_error_text`** text sensor publishes the human-readable meaning of the current code, for example `501: NVS checksum mismatch (saved NVS data rejected; defaults loaded)`. With `ERR_OK` and no flags the payload is `No error`; active warning/alarm bits are appended, so the text also changes when the diagnostic posture changes.
* Both entities are published only when `system_error_code` or `active_alarms` changes. Ordinary relay, pump, mixer and setpoint transitions do not update them.

### 1.2 Telemetry Log Streams

Log records are **never written to the MCU flash**. `T_logger` owns two fixed-size circular buffers in RAM, which are lost on reboot:

* event buffer: 96 records (severity, code, detail, sequence, uptime, latched flag);
* slow buffer: 180 samples at a 10 s cadence (three temperatures, dead-reckoned mixer position, four relay states).

The panels are exposed through the web portal (`/api/log/events`, `/api/log/slow`) and can optionally be streamed to MQTT. Streaming is opt-in via `mqtt_log_enabled`. When a topic override is empty, records are published as JSON lines to `<mqtt_base_topic>/log/event` and `<mqtt_base_topic>/log/slow`; a non-empty `mqtt_log_event_topic` / `mqtt_log_slow_topic` replaces the full topic. The controller tracks a per-stream sequence cursor, so a broker outage does not block logging and the retained RAM backlog is sent once the connection returns. Disabling streaming leaves the buffers available to the web portal only.

`uptime_ms` is the authoritative clock; `wall_time_epoch_s` from NTP is attached as a best-effort annotation when available. The event log includes:

* **Mixer Moves:** start timestamp, direction (`OPEN`/`CLOSE`), energized duration ($\text{ms}$), and surrounding temperatures/encoder delta.
* **Safety & Sensor Events:** every L1 safety trip/recovery and L0 sensor glitch or persistent fault with state context.
* **Configuration Changes:** parameter name, old value, new value, ownership category (Cat 1 / Cat 2 / Cat 3), and config version counter.
* **Demand vs. Physical Divergence Transitions:** every transition where a circuit's effective demand or request differs from its physical relay, or where the derived heater request differs from `heater_phys`, recording the new `divergence_reason` and `system_error_code`.
* **Lifecycle & Mode Transitions:** warm-up flush start/end, Anti-seize start/end, and Test Mode / identification routine start/end.

---

## 2. External Interfaces

### 2.1 MQTT / Home Assistant Contract

#### Heating Demand & Physical Sensor Architecture
* **HA Switches (Commands $\rightarrow$ Controller):** HA commands heating via three per-circuit switches: `heating_loop_1`, `heating_loop_2`, `floor_heating`. There is no heater switch.
* **Keep-alive:** each accepted switch command, including a repeated `ON`, refreshes that circuit's `demand_age_s`. A missed refresh drops the effective demand but keeps `*_intended` set; error `301` is reported while stale.
* **HA Binary Sensors (Controller $\rightarrow$ HA):** the four physical outputs: `Pump 1`, `Pump 2`, `Floor pump`, `Heater` (`*_phys`). Virtual/duplicate entities are not published.
* **Derived heater:** in normal operation L2 sets `heater_requested = floor demand || loop 1 demand || loop 2 demand` after coordination and before the pump-only after-run.

#### Subscribed Command Topics (HA $\rightarrow$ Controller)
Scoped under `<mqtt_base_topic>/cmd/`. Implemented today:
* `<mqtt_base_topic>/cmd/heating_loop_1` (`ON` / `OFF`, repeated `ON` is the keep-alive)
* `<mqtt_base_topic>/cmd/heating_loop_2` (`ON` / `OFF`)
* `<mqtt_base_topic>/cmd/floor_heating` (`ON` / `OFF`)
* `<mqtt_base_topic>/cmd/floor_setpoint` ($^{\circ}\text{C}$, ideal floor forwarding temperature $\rightarrow$ `floor_setpoint_shadow`)

Reserved for the full L2/L3 contract:
* `<mqtt_base_topic>/cmd/dhw_active` (Boolean flag indicating boiler Domestic Hot Water priority is active)
* `<mqtt_base_topic>/cmd/mode` (`heat` / `summer`)
* `<mqtt_base_topic>/cmd/mixer_encoder_pos` ($0.0$–$1.0$, forwarded from WiFi accelerometer/encoder when `pos_source == POS_SRC_HA_GENERIC`)

#### Published State Topics (Controller $\rightarrow$ HA & Observer)
The current HA integration uses one retained topic per entity so MQTT discovery
can bind directly. Implemented today:
* `<mqtt_base_topic>/status` (`online`/`offline`, retained LWT — strictly for HA visibility; triggers no control action)
* `<mqtt_base_topic>/state/heating_loop_1`, `heating_loop_2`, `floor_heating` (effective circuit demand)
* `<mqtt_base_topic>/state/pump_1_physical`, `pump_2_physical`, `floor_pump_physical`, `heater_physical` (physical relay state)
* `<mqtt_base_topic>/state/power_floor`, `power_rad1`, `power_rad2`, `power_mixer` (W)
* `<mqtt_base_topic>/state/mixer_state` (`idle`, `opening`, `closing`)
* `<mqtt_base_topic>/state/temp_boiler`, `temp_return`, `temp_mix` ($^{\circ}\text{C}$ or `unknown`)
* `<mqtt_base_topic>/state/floor_setpoint` ($^{\circ}\text{C}$)
* `<mqtt_base_topic>/state/system_error_code` ($0 = \text{normal}$)
* `<mqtt_base_topic>/state/system_error_text` (human-readable diagnostic text)

Optional telemetry log streams (enabled by `mqtt_log_enabled`, JSON lines):
* `<mqtt_base_topic>/log/slow` (periodic telemetry log stream; override with `mqtt_log_slow_topic`)
* `<mqtt_base_topic>/log/event` (event log stream; override with `mqtt_log_event_topic`)

Reserved aggregate/observer topics:
* `<mqtt_base_topic>/state/temperatures` (`T_boiler`, `T_return_floor`, `T_mix_out`, health flags)
* `<mqtt_base_topic>/state/mixer` (`pos_est_dr`, `pos_encoder`, `pos_source`)
* `<mqtt_base_topic>/state/relays` (intended, demand, requested and physical states for all 3 pumps and the derived heater)
* `<mqtt_base_topic>/state/divergence_reason` (`none`, `floor_delta_t_collapsed`, `overtemp_pause`, `long_pause`, `anti_short_cycle`, `demand_loss`, `after_run`, `dhw_priority_active`)
* `<mqtt_base_topic>/state/mode` (`heat`, `summer`, `anti_seize`, `test_override`) + `test_mode_rem_s` countdown
* `<mqtt_base_topic>/state/alarms` (Active alarm/warning bitmask, flush state, config version)

#### Home Assistant Discovery
Discovery is published retained to
`homeassistant/<component>/<node_id>/<object_id>/config`, with `<node_id>`
derived from the Wi-Fi MAC. The current entity set is:
* 3 switches: `heating_loop_1`, `heating_loop_2`, `floor_heating`;
* 4 binary sensors: `Pump 1`, `Pump 2`, `Floor pump`, `Heater` (all physical);
* 4 power sensors: floor pump, radiator 1 pump, radiator 2 pump, mixer motor;
* 1 mixer-state enum sensor (`idle`/`opening`/`closing`);
* 3 temperature sensors: boiler supply, floor return, mixer forward;
* 1 `number` entity for the floor setpoint;
* 2 diagnostic text sensors: `system_error_code` (numeric code, `0` = normal)
  and `system_error_text` (message and active warning/alarm flags).

The retired `heater`/`pump_*` switches and the old virtual/physical binary
sensors are removed by publishing empty retained discovery payloads on every
discovery cycle.

### 2.2 Local Web Dashboard + WiFi Manager

* **Current state:** the station-mode Wi-Fi client (`hpn::wifi`), MQTT/HA client (`hpn::mqtt`) and LittleFS web portal are implemented. Configuration sections (including Wi-Fi/MQTT parameters) persist in a CRC32-protected NVS blob; blobs from another format version are discarded and compiled defaults are used, with no migration path. The portal provides live state over `/ws`, REST config/log/counter/Test Mode/system/OTA routes, and a bench-only emulation page. Event and slow logs live only in RAM and are served from there; they can optionally be mirrored to MQTT. Relay counters are persisted hourly in hardware builds and stay RAM-only under emulation; counters and the anti-seize schedule also carry CRC32 checksums. The captive-portal provisioning flow and fallback AP are still TODO.
* **Status LEDs:** L4 maps live state to the five addressable LEDs described in `02_hardware_and_l0.md` §1.2. LED 0 shows system/comms health, LED 1 boiler/heater/DHW, LED 2 floor circuit and mixer, and LEDs 3–4 the radiator circuits. L0 owns the NeoPixel write; the animator only supplies RGB frames.
* **Provisioning:** Captive portal and web UI for WiFi credentials, fallback AP settings, and MQTT broker parameters.
* **Configuration Editor:** Full read/write access to **Category 1 (Long-Term Configuration)** parameters; read-only display of **Category 2 (HA)** and **Category 3 (Observer)** parameters.
* **Test Override Mode UI:** Start/stop time-limited Test mode (`test_mode_max_s` countdown), manual output toggles (`pump_floor`, `pump_rad1`, `pump_rad2`, `heater` — Test Mode keeps manual heater control), direct mixer jogging controls (open/close $\pm 2\%$ nudges and stop), and launch buttons for the built-in Observer Identification Test Routines (`05_modes_and_testing.md` §3.3).

### 2.3 Observer Interface (L3 Parameter Ingestion)

* **Reads:** Subscribes to `<mqtt_base_topic>/log/slow`, `<mqtt_base_topic>/log/event`, and `<mqtt_base_topic>/state/#` (or queries HA history).
* **Writes:** Pushes versioned **Category 3 (Control Loop Tuning)** JSON payloads to `<mqtt_base_topic>/config/tuning/set`:
  ```text
  delay_s, gain, ff_boiler, ff_return, kp, ki, rad_coupling_ff,
  start_flush_ms, flush_max_pos, flush_delta_t_ok,
  min_useful_delta_t, floor_pause_duration_s
  ```
* **Validation & Zero Command Authority:** L3 validates every incoming parameter against compiled-in hard safety/plausibility bounds before adopting and persisting to NVS. The Observer has **zero command authority** over relays, setpoints, or operational modes.