# 04 — Command Intake, L2 Control Algorithms & Multi-Circuit Coordination

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (`struct Config` Cat 1–3, `SystemState`), `02_hardware_and_l0.md` (sensors & dead-reckoned position), `06_interfaces_and_logging.md` (HA command intake)  
**Outputs to:** `03_safety_layer_l1.md` (`*_requested` relay states & `mix_target_pos`), `06_interfaces_and_logging.md` (`divergence_reason`, telemetry)

---

## 1. Command Intake, Keep-Alive & Timeout Semantics

### 1.1 Command Contracts (HA Switches $\rightarrow$ Controller Intents)

Communication between Home Assistant (HA) and the controller uses **switches** and **sensors**:

* **Heating-switch intents:** `heating_loop_1`, `heating_loop_2`, and `floor_heating` are HA switches commanding the controller `ON`/`OFF` based on HA's comfort logic. These set `pump_rad1_intended`, `pump_rad2_intended`, and `pump_floor_intended` in `SystemState`.
* **Per-circuit keep-alive:** While a circuit demands heat, HA repeatedly issues that circuit's switch `ON` command (e.g., every $5\text{ minutes}$). Each accepted command — including a repeated `ON` — resets `demand_age_s[circuit] = 0`. The repetition is the liveness signal for that circuit.
* **Keep-alive timeout (`demand_timeout_s`):** If a circuit's `ON` is not refreshed within `demand_timeout_s` (e.g., $2 \times \text{refresh\_time}$), that circuit's effective `*_demand` drops to `false` while `*_intended` remains `ON` as last commanded. The controller reports `divergence_reason = DIV_DEMAND_LOSS` and `ERR_HA_KEEPALIVE_TIMEOUT` (`301`) while any demand is stale. A later `ON` re-arms the demand immediately and clears the error.
* **Derived heater call:** There is no HA heater switch. In normal operation, after the keep-alive and coordination rules, L2 derives `heater_intended = heater_requested = req_floor || req_rad1 || req_rad2`. A pump after-run never keeps the heater on.
* **Board-owned pump after-run (`pump_stop_after_heating_s`):** When a circuit's demand ends (HA turns the switch `OFF` or the keep-alive expires), L2 keeps that circuit's pump requested for `pump_stop_after_heating_s`, then stops it. Each circuit has its own after-run timer. During this window `*_demand = OFF`, `*_requested = ON`, `*_phys = ON` and `divergence_reason = DIV_AFTER_RUN` (when no higher-priority condition is active). HA does not orchestrate this after-run.
* **Explicit switch OFF:** HA turning a heating switch `OFF` ends the demand immediately, but the board still applies that circuit's after-run before the pump physically stops. Set `pump_stop_after_heating_s = 0` to disable the run-on.

### 1.2 Boiler DHW Time-Sharing & Heat-Drop Handling

* **DHW Priority & Time-Sharing (Physical Relay ON, Heat Diverted):**
  The boiler time-shares its capacity between Domestic Hot Water (DHW) and space heating. DHW takes priority and lasts approximately $5$–$15\text{ minutes}$. During this window, **the heater remains requested by the active circuit demands and the physical relay remains ON (`heater_requested = ON`, `heater_phys = ON`), but no heating flow or temperature rise occurs in the distribution circuits** because the boiler internally diverts all heat to the DHW tank.
* **HA Notification (`dhw_active`):** HA notifies the controller of active DHW heating via `dhw_active = true` / `false`. This is the preferred source and remains authoritative when a meter is configured. Optionally, if no meter is configured, L2 can infer the same condition from a boiler-temperature drop while the heater is on (`dhw_infer_enabled`); see §2 rule 7.
* **Stagnation & Cold-Supply Protection (No False Errors):**
  When `dhw_active == true`, L2 adapts to prevent misinterpreting stagnant or dropping $T_{\text{boiler}}$ as a control fault or accumulating integrator windup:
  1. **Integrator Freeze:** The L2 mixer PI integrator (`integrator_val` / `ki`) is frozen (holding the mixer steady or closing it safely if $T_{\text{boiler}}$ drops below setpoint).
  2. **Stagnation/Timeout Suppression:** Low-temperature heating failure codes, thermal collapse errors, and divergence alarms are suppressed. Instead, the controller reports `divergence_reason = DIV_DHW_PRIORITY_ACTIVE`.

### 1.3 Summer Mode Command Rejection

* In **Summer mode** (`mode == MODE_SUMMER`), if HA commands any of the three heating-loop switches `ON`, the controller rejects the command, logs a warning event, and keeps the corresponding `*_intended` state `OFF`. This intentional mismatch triggers an error notification in the HA integration as designed.

---

## 2. Control Algorithms & Multi-Circuit Coordination (L2)

### 2.1 Floor Mixer Control & Anti-Windup

1. **Radiator-Only Floor Protection Rule:**
   * Whenever the heater is active (`heater_phys == ON`) or any radiator pump (`pump_rad1_phys` or `pump_rad2_phys`) is running, **if there is no active floor heating demand (`pump_floor_phys == OFF`)**, L2 commands the floor mixer **fully closed ($0.0$)** to prevent parasitic thermosiphon or unmixed hot water migration into the floor slab.
   * When floor heating is active (`pump_floor_phys == ON`), the mixer regulates `T_mix_out` toward `floor_setpoint_shadow`.
2. **Supply-Temperature Gating & Integrator Anti-Windup:**
   * Whenever $T_{\text{boiler}} < \text{floor\_setpoint\_shadow} + \text{deadband\_temp}$ (e.g., cold boiler, warm-up phase, `dhw_active == true`, or after-run), or when `heater_phys == OFF`, or during the **warm-up flush** (§2.3), the L2 **integrator (`ki`) is frozen** and opening beyond the feedforward position estimate is inhibited. This prevents integrator windup from driving the mixer $100\%$ open while supply water is cool.
3. **Predictive Closed-Loop Evaluation (Discrete Smith-Predictor):**
   * Predicts $T_{\text{mix\_out}}$ at $t + \text{delay\_s}$ using the current position estimate (`pos_est_dr`), the feedforward blending model
     $$T_{\text{ff}} = \text{pos\_est\_dr} \cdot \text{ff\_boiler} \cdot T_{\text{boiler}} + (1 - \text{pos\_est\_dr}) \cdot \text{ff\_return} \cdot T_{\text{return\_floor}} - \text{rad\_coupling\_ff} \cdot [(\text{pump\_rad1\_phys} \lor \text{pump\_rad2\_phys}) ? 1 : 0]$$
     and the measured trend $dT/dt$ extrapolated over $\text{delay\_s}$ — if a temperature trajectory is already underway toward the setpoint, L2 **waits** rather than overshooting.
   * If the predicted error at $t + \text{delay\_s}$ is within `deadband_temp`, the target is held at the current position. Otherwise L2 computes the desired position and sets `mix_target_pos`; a per-tick `max_step` clamp ($10\%$) limits how fast the target slews. L2 never issues pulses: L1 drives **one continuous servo move** to `mix_target_pos` and stops on arrival (see [`02_hardware_and_l0.md`](02_hardware_and_l0.md) §2.2). If the new target demands the opposite direction, L1 stops, waits the $50\text{ ms}$ direction-change dead-time and reverses immediately — there is no cooldown and no `max_pulse_ms`.
4. **Opening window:** `mix_max_open_pct` (default **85 %**) is an absolute upper bound on the mixer opening. L2 clamps every computed and held target to the effective ceiling (which may be lower when the dynamic two-point maximum is enabled); L1 is the authoritative enforcement point and refuses to open past it even for a maintenance sweep. Lowering the ceiling while the valve is above it drives the valve back down. End-stop homing to $0.0$ and all closing moves are unaffected. The portal clamps manual test targets to the same value.
5. **Minimum opening:** `mix_min_open_pct` (default **40 %**, the old controller's floor minimum) applies only to normal L2 floor-demand control. It is bypassed by closing moves, no-demand close, DHW handling, the warm-up flush cap, safety close, homing and the over-temperature reaction, all of which must be able to reach $0.0$.
6. **Setpoint + Δ reaction:** when $T_{\text{mix\_out}} \ge \text{setpoint} + \Delta$ (`mix_overshoot_delta_c`, default 3 K), L2 drives to `mix_overshoot_target_pct` (default 40 %) and holds it until the temperature falls to $\text{setpoint} - \text{recovery}$ (`mix_overshoot_recovery_c`, default 1 K). It raises `WARN_MIX_OVERSHOOT`; it is a soft control reaction, not a safety latch, so L1's hard over-temperature rule remains authoritative.
7. **Optional inferred DHW:** when no meter is configured and `dhw_infer_enabled` is set, a boiler-temperature drop of `dhw_infer_drop_c` within `dhw_infer_window_s` while `heater_phys` is on sets `dhw_inferred`. The inference ends when the temperature recovers within `dhw_infer_recovery_c` of the pre-drop peak. If it lasts longer than `dhw_infer_max_s` (default 1800 s), L2 releases the floor and raises `WARN_DHW_INFER_TIMEOUT`. The meter signal is never overridden: `dhw_active = dhw_meter_active || dhw_inferred`.

### 2.2 Mixer Position Tracking & Abstract Encoder Consistency Checks

* **Primary Tracking:** Internal position `pos_est_dr` ($[0.0, 1.0]$) is tracked via dead reckoning (integrating open/close pulse durations) combined with end-stop zero-homing when driven to $0.0$.
* **Abstracted Encoder Interface:** Position feedback (`pos_encoder`) is hardware-agnostic (`POS_SRC_HA_GENERIC` via MQTT or `POS_SRC_I2C` via local $\text{I}^2\text{C}$).
* **Staleness Supervision:** If $(t - \text{pos\_encoder\_ms}) > \text{encoder\_staleness\_s}$ (e.g. $300\text{ s}$), the controller marks `pos_encoder_valid = false` and raises `WARN_ENCODER_STALE`.
* **Periodic Consistency Check:** Every `encoder_check_interval_s` (e.g., $3600\text{ s}$) or after `encoder_check_pulses` (e.g., $100$ pulses), the controller compares `pos_est_dr` against `pos_encoder` *if valid encoder data is available*:
  * If $|\text{pos\_est\_dr} - \text{pos\_encoder}| > \text{recalc\_threshold}$, the controller synchronizes `pos_est_dr = pos_encoder`, raises `WARN_ENCODER_DIVERGED`, and logs a recalibration event.
  * If encoder data is missing or stale (`pos_encoder_valid == false`) when a check is due, the controller raises `WARN_ENCODER_STALE` and defers the check until valid encoder data arrives.

### 2.3 Floor Circuit Warm-Up Flush

* **Trigger:** `pump_floor` starts from `OFF`, or restarts after being `OFF` long enough for `T_return_floor` to cool or converge (`flush_state = FLUSHING`).
* **Behavior:**
  * `pump_floor` runs; if `pos_est_dr > flush_max_pos`, the mixer clamps down to `flush_max_pos` (e.g., $0.20 = 20\%$, not fully closed).
  * **Duration:** `start_flush_ms` (e.g., $20$–$60\text{ s}$) to flush the stagnant water column past `T_return_floor`.
  * **Gating during Flush:** Closed-loop mixer pulses and integrator accumulation are held, return-based feedforward (`ff_return`) is inhibited, and `t_ret_flush_valid = false`. **The heater relay (`heater_phys`) is left untouched during flush** (follows active demand).
  * **Completion:** After `start_flush_ms` expires (or early stabilization per `flush_delta_t_ok`), `t_ret_flush_valid = true`, `flush_state = IDLE`, and normal closed-loop mixer control resumes.

### 2.4 Multi-Circuit Heater & Pump Coordination

1. **Heater Derivation:**
   * After the keep-alive, summer/anti-seize and coordination rules, L2 computes the base requests `req_floor`, `req_rad1`, `req_rad2` and derives the heater call as `req_floor || req_rad1 || req_rad2`.
   * The heater request is set before the pump after-run is applied: an after-run circulates residual heat with the heater off.
2. **Board-Owned Pump After-Run:**
   * When a circuit demand ends while that circuit's pump is requested or physically running, L2 starts that circuit's after-run (`after_run_age_s[circuit]`).
   * The circuit's pump stays requested until `pump_stop_after_heating_s` expires, then stops. The heater never stays on for an after-run.
   * `divergence_reason = DIV_AFTER_RUN` is reported while an after-run is the only active condition. HA does not orchestrate this; it only observes the switch state and the physical pump sensor.
3. **Floor $\Delta T$-Collapse Rule (Early Floor Shutdown & Planned Pause):**
   * When the floor circuit is running post-flush (`t_ret_flush_valid == true`) and its temperature drop persistently collapses:
     $$T_{\text{mix\_out}} - T_{\text{return\_floor}} < \text{min\_useful\_delta\_t}$$
     the floor slab is thermally saturated.
   * **Action on Floor Circuit:** L2 sets `pump_floor_requested = OFF` (keeping `pump_floor_intended = ON`), drives the mixer to $0.0$, sets `flush_state = PAUSED_DELTA_T`, and sets `divergence_reason = DIV_FLOOR_DELTA_T_COLLAPSED`.
   * **Action on Shared Heater:** If **neither radiator pump is requested or running** (`req_rad1 == false && req_rad2 == false`), the derived heater call drops for the planned pause duration `floor_pause_duration_s` (optimized by the Observer). If any radiator circuit is active, `heater_phys` remains `ON` to serve the radiators.
   * **Recovery:** After `floor_pause_duration_s` expires, L2 restarts `pump_floor` and runs a Warm-Up Flush (§2.3) to sample whether a useful $\Delta T$ has returned.
4. **"Heater ON + All Pumps OFF" Policy (`long_pause_s`):**
   * Having `heater_phys == ON` while all three pumps (`pump_floor_phys`, `pump_rad1_phys`, `pump_rad2_phys`) are `OFF` is permitted by L1 (relying on the boiler's internal modulation/cutout) and raises a diagnostic warning (`WARN_HEATER_NO_PUMP`), not a safety trip.
   * If this state persists beyond `long_pause_s`, L2 drops the heater request through `DIV_LONG_PAUSE`.

---

## 3. Multi-Circuit Coordination Summary Table

| Situation | Heater `requested` / `phys` | Floor `demand` / `phys` | Radiators `demand` / `phys` | Mixer Target | Notes & Divergence Reason |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Normal heating (all/any)** | derived `ON` / `ON` | per HA / same | per HA / same | Closed ($0.0$) if floor demand OFF, else L2 loop | Standard operation (`DIV_NONE`) |
| **DHW Priority Active (`dhw_active = true`)** | derived `ON` / **`ON`** | per HA / same | per HA / same | Regulates or holds | Boiler relay ON, heat diverted internally to DHW; integrator frozen (`DIV_DHW_PRIORITY_ACTIVE`) |
| **Board after-run** | `OFF` / `OFF` | `OFF` / **`ON`** until timeout | `OFF` / **`ON`** until timeout | Holds or closes | Pump-only run-on after a demand ends (`DIV_AFTER_RUN`) |
| **Floor $\Delta T$ collapsed (Radiators OFF)** | `OFF` / **`OFF`** | `ON` / **`OFF`** | `OFF` / `OFF` | **Closed ($0.0$)** | Floor saturated $\rightarrow$ pauses floor pump & heater for `floor_pause_duration_s` (`DIV_FLOOR_DELTA_T_COLLAPSED`) |
| **Floor $\Delta T$ collapsed (Radiator ON)** | `ON` / `ON` | `ON` / **`OFF`** | `ON` / `ON` | **Closed ($0.0$)** | Stops floor pump & closes mixer; boiler stays ON for radiators (`DIV_FLOOR_DELTA_T_COLLAPSED`) |
| **Floor Flush phase** | derived `ON` / same | `ON` / `ON` | per HA / same | Capped at `flush_max_pos` | Heater relay untouched by flush (`DIV_NONE`) |
| **Heater ON, all pumps OFF** | `ON` / `ON` ($\rightarrow$ **`OFF`** after `long_pause_s`) | `OFF` / `OFF` | `OFF` / `OFF` | **Closed ($0.0$)** | Permitted with `WARN_HEATER_NO_PUMP`; cuts heater after `long_pause_s` (`DIV_LONG_PAUSE`) |
| **Demand keep-alive expired** | **`OFF` / `OFF`** | `ON` (intent) / `OFF` demand / **`OFF`** after after-run | same per circuit | **Closed ($0.0$)** | Demand drops at `demand_timeout_s` (`DIV_DEMAND_LOSS`, `301`); pump stops after `pump_stop_after_heating_s` |
| **Summer Mode Command** | **`OFF` / `OFF`** (warning) | **`OFF` / `OFF`** (warning) | **`OFF` / `OFF`** (warning) | **Closed ($0.0$)** | Rejects command; `*_intended` stays `OFF` to trigger HA error |
| **L1 Floor Overtemp ($\ge t_{\text{max\_hard}}$)** | derived / **unchanged** | per HA / **`OFF`** | per HA / same | **Force Closed ($0.0$)** | Stops `pump_floor_phys` & closes mixer; resumes when $T_{\text{mix\_out}}$ drops (`DIV_OVERTEMP_PAUSE`) |