# 03 — Safety Layer (L1) Arbitration

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (Category 1 safety bounds & `SystemState`), `02_hardware_and_l0.md` (validated sensor snapshots), `04_control_and_coordination_l2.md` (`*_requested` actuator states & pulse requests)  
**Outputs to:** `02_hardware_and_l0.md` (final arbitrated `*_phys` relay and mixer commands), `06_interfaces_and_logging.md` (alarms, `divergence_reason`, error codes)

---

## 1. L1 Execution Contract

L1 evaluates deterministically on every tick of `T_safety` (Core 1, highest priority, $500\text{ ms}$–$1\text{ s}$ interval) and **arbitrates all final relay and actuator outputs** (`heater_phys`, `pump_floor_phys`, `pump_rad1_phys`, `pump_rad2_phys`, and mixer pulse permissions `mix_pulse_dir_phys` / `mix_pulse_ms_phys`).

* Nothing reaches L0 hardware drivers without passing L1 arbitration.
* L1 continues executing and enforcing safe states even if L2 (`T_control`) hangs or WiFi/MQTT (`T_comms`) is down.

---

## 2. Deterministic Safety Rules

### 2.1 Hard Floor Over-Temperature ($T_{\text{mix\_out}} \ge t_{\text{max\_hard}}$)

* **Immediate Action:** Forces **`pump_floor_phys = OFF`** (to immediately stop pumping scalding water into the floor slab while the slow motorized valve is still open) and commands the **mixer toward closed ($0.0$)** via `mix_pulse_dir_phys = -1`.
* **Diagnostics:** Sets `divergence_reason = DIV_OVERTEMP_PAUSE`, sets `active_alarms |= ALARM_OVERTEMP_HARD`, sets `system_error_code = ERR_L1_HARD_OVERTEMP`, and emits an immediate event log.
* **Shared Heater Policy:** **`heater_phys` is not forced `OFF`** by a floor over-temperature trip (unless required by another rule), because the floor circuit will resume once $T_{\text{mix\_out}}$ cools and radiator circuits (`pump_rad1`, `pump_rad2`) may actively need the boiler. (If no radiators are active, Rule §2.7 / L2 `long_pause_s` will subsequently shut off the heater).
* **Recovery:** Once $T_{\text{mix\_out}} < (t_{\text{max\_warn}} - t_{\text{overtemp\_hysteresis}})$ (and the mixer has reached the closed position), `pump_floor_phys` is permitted to resume following L2 requests, and `ALARM_OVERTEMP_HARD` is cleared.

### 2.2 High-Temperature Warning Band ($t_{\text{max\_warn}} \le T_{\text{mix\_out}} < t_{\text{max\_hard}}$)

* Inhibits any mixer `OPEN` pulses requested by L2 or Test mode (`mix_pulse_dir_phys` forced to `0` or `-1`).
* Permits mixer `CLOSE` pulses.
* Raises high-temperature warning flag (`active_alarms |= WARN_OVERTEMP_HIGH`).

### 2.3 Temperature Gradient Cutoff ($dT/dt > \text{grad\_limit}$)

* Evaluated against `T_mix_out.gradient_k_per_s` computed from the steady 1-Wire polling stream (`02_hardware_and_l0.md`).
* Immediately aborts any active mixer `OPEN` pulse in L0 and blocks further `OPEN` pulses until $dT/dt \le \text{grad\_limit}$.
* Raises gradient cutoff flag (`active_alarms |= WARN_GRADIENT_CUTOFF`).

### 2.4 Persistent Sensor Fault Postures (`is_valid == false`)

When L0 flags a sensor as persistently invalid (after `sensor_error_threshold` consecutive rejected reads or staleness timeout):

1. **`T_mix_out` Fault (Primary Safety & Control Sensor):**
   * Immediately drives **mixer closed ($0.0$)**.
   * Once closed, unconditionally forces **`pump_floor_phys = OFF`** (floor pump must never run without its primary safety sensor, preventing unmonitored scalding water circulation into the slab).
   * Raises critical sensor alarm (`active_alarms |= ALARM_SENSOR_FAULT_MIX`, `system_error_code = ERR_SENSOR_MIX_LOST`).
2. **`T_return_floor` Fault:**
   * Inhibits return-temperature feedforward (`ff_return`) and disables the floor $\Delta T$-collapse shutdown rule in L2.
   * Permits L2 to regulate `T_mix_out` using feedback + boiler feedforward only; raises sensor warning (`active_alarms |= WARN_SENSOR_FAULT_RET`, `system_error_code = ERR_SENSOR_RET_LOST`).
3. **`T_boiler` Fault:**
   * Inhibits boiler feedforward (`ff_boiler`) and supply-temperature anti-windup gating, substituting a conservative maximum mixer opening cap; raises sensor warning (`active_alarms |= WARN_SENSOR_FAULT_BOILER`, `system_error_code = ERR_SENSOR_BOILER_LOST`).
4. **Radiator Independence:**
   * Radiator circuits (`pump_rad1_phys`, `pump_rad2_phys`) do not depend on local floor sensors and continue operating normally during local floor sensor faults.

### 2.5 Summer Mode Heater Lockout

* Whenever `mode == MODE_SUMMER` or `mode == MODE_ANTI_SEIZE`, L1 unconditionally forces `heater_phys = OFF`.

### 2.6 Boiler Anti-Short-Cycle Backstop

* Prevents `heater_phys` from transitioning `OFF` $\rightarrow$ `ON` until at least `boiler_min_off_ms` has elapsed since the last `ON` $\rightarrow$ `OFF` transition.
* On controller cold boot, `boiler_min_off_ms` is initialized as active to protect the boiler burner against rapid cycling during power brownouts.
* While held off, sets `divergence_reason = DIV_ANTI_SHORT_CYCLE` (keeping `heater_intended = ON`).

### 2.7 Supervision Timeout Backstops

* **Heater Keep-Alive Backstop:** Verifies that `heater_phys == OFF` whenever `keepalive_age_s >= demand_timeout_s` (outside of active local Test Override mode), enforcing `divergence_reason = DIV_DEMAND_LOSS`.
* **Pump After-Heating Backstop:** Verifies that `pump_floor_phys == OFF`, `pump_rad1_phys == OFF`, and `pump_rad2_phys == OFF` whenever `after_heating_timer_s >= pump_stop_after_heating_s` and `heater_phys == OFF` (outside of active Anti-seize or local Test Override mode), enforcing `divergence_reason = DIV_AFTER_HEATING_TIMEOUT`.