# 04 — Command Intake, L2 Control Algorithms & Multi-Circuit Coordination

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (`struct Config` Cat 1–3, `SystemState`), `02_hardware_and_l0.md` (sensors & dead-reckoned position), `06_interfaces_and_logging.md` (HA command intake)  
**Outputs to:** `03_safety_layer_l1.md` (`*_requested` relay states & `mix_target_pos`), `06_interfaces_and_logging.md` (`divergence_reason`, telemetry)

---

## 1. Command Intake, Keep-Alive & Timeout Semantics

### 1.1 Command Contracts (HA Switches $\rightarrow$ Controller Intents)

Communication between Home Assistant (HA) and the controller uses **switches** and **sensors**:

* **Switch Intents:** `heater`, `pump_floor`, `pump_rad1`, and `pump_rad2` are HA switches commanding the controller `ON`/`OFF` based on HA's comfort logic. These set `*_intended` in `SystemState`.
* **Heater Keep-Alive:** While heat is demanded, HA repeatedly issues the `heater` switch `ON` command (e.g., every $5\text{ minutes}$). Each reception resets `keepalive_age_s = 0` and signals that the HA integration/connection is alive.
* **Keep-Alive Timeout (`demand_timeout_s`):** If no heater keep-alive message is received within `demand_timeout_s` (e.g., $\text{refresh\_time} + 1\text{ min}$, or $2 \times \text{refresh\_time} + 1\text{ min}$), the controller declares HA communication lost and immediately shuts down the heater (`heater_requested = OFF`, `heater_phys = OFF`, `heater_intended` remains `ON` as last commanded, `divergence_reason = DIV_DEMAND_LOSS`).
* **Staged Pump Shutdown (`pump_stop_after_heating_s`):** When the heater keep-alive expires, or when HA turns the `heater` switch `OFF` while pumps remain commanded `ON`, the after-heating timer (`after_heating_timer_s`) starts. Running pumps continue operating until `after_heating_timer_s >= pump_stop_after_heating_s`, after which all pumps are stopped (`pump_*_phys = OFF`, `pump_*_intended` remains `ON`, `divergence_reason = DIV_AFTER_HEATING_TIMEOUT`). Internal layers (such as L2 flush or L1 cooling safety) may govern floor pump state within safety rules.
* **Explicit Pump Shutdown by HA:** If HA explicitly commands a pump off (`pump_*_intended = OFF`), the controller turns that pump `OFF` **immediately**, regardless of `after_heating_timer_s`.
* **Pumps Switched ON Without Heater:** If HA switches any pump `ON` while `heater` is `OFF`, the same `pump_stop_after_heating_s` timer starts immediately. There is no keep-alive for pumps alone; pumps cannot run indefinitely without active heater demand.

### 1.2 Boiler DHW Time-Sharing & Heat-Drop Handling

* **DHW Priority & Time-Sharing (Physical Relay ON, Heat Diverted):**
  The boiler time-shares its capacity between Domestic Hot Water (DHW) and space heating. DHW takes priority and lasts approximately $5$–$15\text{ minutes}$. During this window, **the heater switch and physical relay remain ON (`heater_intended = ON`, `heater_phys = ON`), but no heating flow or temperature rise occurs in the distribution circuits** because the boiler internally diverts all heat to the DHW tank.
* **HA Notification (`dhw_active`):** HA notifies the controller of active DHW heating via `dhw_active = true` / `false`.
* **Stagnation & Cold-Supply Protection (No False Errors):**
  When `dhw_active == true`, L2 adapts to prevent misinterpreting stagnant or dropping $T_{\text{boiler}}$ as a control fault or accumulating integrator windup:
  1. **Integrator Freeze:** The L2 mixer PI integrator (`integrator_val` / `ki`) is frozen (holding the mixer steady or closing it safely if $T_{\text{boiler}}$ drops below setpoint).
  2. **Stagnation/Timeout Suppression:** Low-temperature heating failure codes, thermal collapse errors, and divergence alarms are suppressed. Instead, the controller reports `divergence_reason = DIV_DHW_PRIORITY_ACTIVE`.

### 1.3 Summer Mode Command Rejection

* In **Summer mode** (`mode == MODE_SUMMER`), if HA commands `heater` or any `pump_*` switch `ON`, the controller rejects the command, logs a warning event, and keeps the corresponding `*_intended` echo sensor `OFF`. This intentional mismatch triggers an error notification in the HA integration as designed.

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
4. **Opening Ceiling:** `mix_max_open_pct` (default **85 %**) is an absolute upper bound on the mixer opening. L2 clamps every computed and held target to it; L1 is the authoritative enforcement point and refuses to open past the ceiling even for a maintenance sweep. Lowering the ceiling while the valve is above it drives the valve back down. End-stop homing to $0.0$ and all closing moves are unaffected. The portal clamps manual test targets to the same value.

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

1. **Normal After-Run (HA-Driven):**
   * When a heating cycle finishes, HA commands `heater = OFF` while keeping the relevant circuit pump(s) `ON` to circulate residual heat, then commands the pump(s) `OFF`. Supervised by `pump_stop_after_heating_s` (§1.1).
2. **Floor $\Delta T$-Collapse Rule (Early Floor Shutdown & Planned Pause):**
   * When the floor circuit is running post-flush (`t_ret_flush_valid == true`) and its temperature drop persistently collapses:
     $$T_{\text{mix\_out}} - T_{\text{return\_floor}} < \text{min\_useful\_delta\_t}$$
     the floor slab is thermally saturated.
   * **Action on Floor Circuit:** L2 sets `pump_floor_requested = OFF` (keeping `pump_floor_intended = ON`), drives the mixer to $0.0$, sets `flush_state = PAUSED_DELTA_T`, and sets `divergence_reason = DIV_FLOOR_DELTA_T_COLLAPSED`.
   * **Action on Shared Heater:** If **neither radiator pump is running** (`pump_rad1_phys == OFF && pump_rad2_phys == OFF`), L2 also sets `heater_requested = OFF` (keeping `heater_intended = ON`) for the planned pause duration `floor_pause_duration_s` (optimized by the Observer). If any radiator pump is running, `heater_phys` remains `ON` to serve the radiators.
   * **Recovery:** After `floor_pause_duration_s` expires, L2 restarts `pump_floor` and runs a Warm-Up Flush (§2.3) to sample whether a useful $\Delta T$ has returned.
3. **"Heater ON + All Pumps OFF" Policy (`long_pause_s`):**
   * Having `heater_phys == ON` while all three pumps (`pump_floor_phys`, `pump_rad1_phys`, `pump_rad2_phys`) are `OFF` is permitted by L1 (relying on the boiler's internal modulation/cutout) and raises a diagnostic warning (`WARN_HEATER_NO_PUMP`), not a safety trip.
   * If this state persists beyond `long_pause_s`, L2 sets `heater_requested = OFF` (`heater_intended` remains `ON`, `divergence_reason = DIV_LONG_PAUSE`).

---

## 3. Multi-Circuit Coordination Summary Table

| Situation | `heater_intended` / `heater_phys` | `pump_floor_intended` / `pump_floor_phys` | `pump_rad1,2_intended` / `pump_rad1,2_phys` | Mixer Target | Notes & Divergence Reason |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Normal heating (all/any)** | `ON` / `ON` | per HA / same | per HA / same | Closed ($0.0$) if `pump_floor=OFF`, else L2 loop | Standard operation (`DIV_NONE`) |
| **DHW Priority Active (`dhw_active = true`)** | `ON` / **`ON`** | per HA / same | per HA / same | Regulates or holds | Boiler relay ON, heat diverted internally to DHW; integrator frozen (`DIV_DHW_PRIORITY_ACTIVE`) |
| **After-run (HA-driven)** | `OFF` / `OFF` | per HA / same | per HA / same | Holds or closes | Supervised by `pump_stop_after_heating_s` (`DIV_NONE`) |
| **Floor $\Delta T$ collapsed (Radiators OFF)** | `ON` / **`OFF`** | `ON` / **`OFF`** | `OFF` / `OFF` | **Closed ($0.0$)** | Floor saturated $\rightarrow$ pauses floor pump & heater for `floor_pause_duration_s` (`DIV_FLOOR_DELTA_T_COLLAPSED`) |
| **Floor $\Delta T$ collapsed (Radiator ON)** | `ON` / `ON` | `ON` / **`OFF`** | `ON` / `ON` | **Closed ($0.0$)** | Stops floor pump & closes mixer; boiler stays ON for radiators (`DIV_FLOOR_DELTA_T_COLLAPSED`) |
| **Floor Flush phase** | per HA / same | `ON` / `ON` | per HA / same | Capped at `flush_max_pos` | Heater relay untouched by flush (`DIV_NONE`) |
| **Heater ON, all pumps OFF** | `ON` / `ON` ($\rightarrow$ **`OFF`** after `long_pause_s`) | `OFF` / `OFF` | `OFF` / `OFF` | **Closed ($0.0$)** | Permitted with `WARN_HEATER_NO_PUMP`; cuts heater after `long_pause_s` (`DIV_LONG_PAUSE`) |
| **Demand keep-alive expired** | **`ON` / `OFF`** | `ON` / **`OFF`** (after timeout) | `ON` / **`OFF`** (after timeout) | **Closed ($0.0$)** | Heater off at `demand_timeout_s` (`DIV_DEMAND_LOSS`); pumps off at `pump_stop_after_heating_s` (`DIV_AFTER_HEATING_TIMEOUT`) |
| **Summer Mode Command** | **`OFF` / `OFF`** (warning) | **`OFF` / `OFF`** (warning) | **`OFF` / `OFF`** (warning) | **Closed ($0.0$)** | Rejects command; `*_intended` stays `OFF` to trigger HA error |
| **L1 Floor Overtemp ($\ge t_{\text{max\_hard}}$)** | per HA / **unchanged** | per HA / **`OFF`** | per HA / same | **Force Closed ($0.0$)** | Stops `pump_floor_phys` & closes mixer; resumes when $T_{\text{mix\_out}}$ drops (`DIV_OVERTEMP_PAUSE`) |