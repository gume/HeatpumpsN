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
  * `301` (`ERR_HA_KEEPALIVE_TIMEOUT`): HA heater keep-alive message missed beyond `demand_timeout_s`.
  * `302` (`ERR_AFTER_HEAT_TIMEOUT`): Pump after-heating run expired without heat demand (`pump_stop_after_heating_s`).
  * `401` (`ERR_RELAY_FAULT_WELDED`): Welded relay contact detected by pump power sensor.
  * `501` (`ERR_NVS_DATA_CORRUPTED`): Configuration CRC check failed in NVS; compiled-in safety defaults loaded.

### 1.2 Telemetry Log Streams

Two distinct log streams are buffered in `T_logger` and published via MQTT (`uptime_ms` is the authoritative clock; `wall_time_epoch_s` from NTP is attached as a best-effort annotation):

1. **Slow Log (Periodic — every $10$–$30\text{ s}$):**
   ```text
   uptime_ms, wall_time, mode, T_boiler, T_return_floor, T_mix_out, sensor_health_flags,
   pos_est_dr, pos_encoder, pos_source, pump_floor_intended/phys, pump_rad1_intended/phys,
   pump_rad2_intended/phys, pump_power[3], heater_intended/phys, divergence_reason_code,
   dhw_active, system_error_code, flush_state, active_alarms, integrator_val,
   keepalive_age_s, after_heating_timer_s
   ```
2. **Event Log (Immediate on Occurrence):**
   * **Mixer Pulses:** Start timestamp, direction (`OPEN`/`CLOSE`), duration ($\text{ms}$), $T_{\text{boiler}}$, $T_{\text{return\_floor}}$, $T_{\text{mix\_out}}$ before/after, and encoder delta.
   * **Safety & Sensor Events:** Every L1 safety trip/recovery and L0 sensor glitch (`-127 degC`, `+85 degC`, CRC, slew jump) or persistent fault with full state context.
   * **Configuration Changes:** Parameter name, old value, new value, ownership category (Cat 1 / Cat 2 / Cat 3), and config version counter.
   * **Command vs. Physical Divergence Transitions:** Every transition where `heater_intended != heater_phys` or `pump_*_intended != pump_*_phys`, recording the new `divergence_reason` and `system_error_code`.
   * **Lifecycle & Mode Transitions:** Warm-up flush start/end, Anti-seize start/end, and Test Mode / identification routine start/end.

---

## 2. External Interfaces

### 2.1 MQTT / Home Assistant Contract

#### Dual-State Switch & Binary Sensor Architecture
* **HA Switches (Commands $\rightarrow$ Controller):** HA commands heating via switches (`pump_floor`, `pump_rad1`, `pump_rad2`, `heater`).
* **HA Binary Sensors (Controller $\rightarrow$ HA):**
  1. **Intended State Echo (`*_intended`):** Reflects the accepted command state. For `heater` and `pump_floor`, `*_intended` remains `ON` even when the controller internally pauses the relay due to safety, $\Delta T$ collapse, anti-short-cycle, or DHW priority — preventing false HA communication error alerts. (In Summer mode, rejected `ON` commands keep `*_intended = OFF` to intentionally alert HA).
  2. **Physical State (`*_phys`):** Reports the true hardware relay state (`heater_phys`, `pump_floor_phys`, `pump_rad1_phys`, `pump_rad2_phys`) for monitoring and energy accounting.

#### Subscribed Command Topics (HA $\rightarrow$ Controller)
Scoped under `<mqtt_base_topic>/cmd/`:
* `<mqtt_base_topic>/cmd/pump_floor` (`ON` / `OFF`)
* `<mqtt_base_topic>/cmd/pump_rad1` (`ON` / `OFF`)
* `<mqtt_base_topic>/cmd/pump_rad2` (`ON` / `OFF`)
* `<mqtt_base_topic>/cmd/heater` (`ON` repeated periodically as keep-alive / `OFF`)
* `<mqtt_base_topic>/cmd/floor_setpoint` ($^{\circ}\text{C}$, ideal floor forwarding temperature $\rightarrow$ `floor_setpoint_shadow`)
* `<mqtt_base_topic>/cmd/dhw_active` (Boolean flag indicating boiler Domestic Hot Water priority is active)
* `<mqtt_base_topic>/cmd/mode` (`heat` / `summer`)
* `<mqtt_base_topic>/cmd/mixer_encoder_pos` ($0.0$–$1.0$, forwarded from WiFi accelerometer/encoder when `pos_source == POS_SRC_HA_GENERIC`)

#### Published State Topics (Controller $\rightarrow$ HA & Observer)
Scoped under `<mqtt_base_topic>/state/` and `<mqtt_base_topic>/log/`:
* `<mqtt_base_topic>/state/temperatures` (`T_boiler`, `T_return_floor`, `T_mix_out`, health flags)
* `<mqtt_base_topic>/state/mixer` (`pos_est_dr`, `pos_encoder`, `pos_source`)
* `<mqtt_base_topic>/state/relays` (Intended and physical states for all 3 pumps and heater)
* `<mqtt_base_topic>/state/system_error_code` ($0 = \text{normal}$)
* `<mqtt_base_topic>/state/divergence_reason` (`none`, `floor_delta_t_collapsed`, `overtemp_pause`, `long_pause`, `anti_short_cycle`, `demand_loss`, `after_heating_timeout`, `dhw_priority_active`)
* `<mqtt_base_topic>/state/mode` (`heat`, `summer`, `anti_seize`, `test_override`) + `test_mode_rem_s` countdown
* `<mqtt_base_topic>/state/alarms` (Active alarm/warning bitmask, flush state, config version)
* `<mqtt_base_topic>/log/slow` (Periodic telemetry log stream)
* `<mqtt_base_topic>/log/event` (Immediate event log stream)
* `<mqtt_base_topic>/status` (MQTT LWT: `online`/`offline` — strictly for HA visibility; triggers no control action)

### 2.2 Local Web Dashboard + WiFi Manager

* **Provisioning:** Captive portal and web UI for WiFi credentials, fallback AP settings, and MQTT broker parameters.
* **Configuration Editor:** Full read/write access to **Category 1 (Long-Term Configuration)** parameters; read-only display of **Category 2 (HA)** and **Category 3 (Observer)** parameters.
* **Test Override Mode UI:** Start/stop time-limited Test mode (`test_mode_max_s` countdown), manual relay toggles (`pump_floor`, `pump_rad1`, `pump_rad2`, `heater`), direct mixer jogging controls (open/close step pulses), and launch buttons for the built-in Observer Identification Test Routines (`05_modes_and_testing.md` §3.3).

### 2.3 Observer Interface (L3 Parameter Ingestion)

* **Reads:** Subscribes to `<mqtt_base_topic>/log/slow`, `<mqtt_base_topic>/log/event`, and `<mqtt_base_topic>/state/#` (or queries HA history).
* **Writes:** Pushes versioned **Category 3 (Control Loop Tuning)** JSON payloads to `<mqtt_base_topic>/config/tuning/set`:
  ```text
  delay_s, gain, ff_boiler, ff_return, kp, ki, rad_coupling_ff,
  start_flush_ms, flush_max_pos, flush_delta_t_ok,
  min_useful_delta_t, floor_pause_duration_s
  ```
* **Validation & Zero Command Authority:** L3 validates every incoming parameter against compiled-in hard safety/plausibility bounds before adopting and persisting to NVS. The Observer has **zero command authority** over relays, setpoints, or operational modes.