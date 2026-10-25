# 05 — Operational Modes, Anti-Seize & Identification Test Routines

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (`OperationalMode`, timers), `04_control_and_coordination_l2.md` (command pipeline)  
**Outputs to:** `03_safety_layer_l1.md` & `04_control_and_coordination_l2.md` (emulated commands, mixer jog pulses), `06_interfaces_and_logging.md` (test logs for Observer)

---

## 1. Heat Mode (`MODE_HEAT` — Normal Operation)

* Standard closed-loop and multi-circuit coordination mode following Home Assistant commands across all three hydraulic circuits (`pump_floor`, `pump_rad1`, `pump_rad2`, and shared `heater`).

---

## 2. Summer (Dry) Mode & Anti-Seize Exercise (`MODE_SUMMER` / `MODE_ANTI_SEIZE`)

* **Summer Mode Activation:** Commanded by HA (`summer_mode = true`) and persisted in NVS.
* **Command Rejection:** Any incoming heating (`heater = ON`) or standard comfort pump command from HA is rejected with a warning log, and `*_intended` remains `OFF` so HA detects the mode mismatch.
* **Autonomous Anti-Seize Exercise (`MODE_ANTI_SEIZE`):**
  * Every `anti_seize_interval_s` ($\sim 30\text{ days}$), while in Summer mode, the controller autonomously enters `MODE_ANTI_SEIZE`.
  * **Safety Lockout:** L1 strictly forces `heater_phys = OFF`.
  * **Exercise Sequence:** Runs `pump_floor`, `pump_rad1`, and `pump_rad2` briefly (e.g., $60\text{ s}$) and sweeps the mixer across its full mechanical stroke ($0\% \rightarrow 100\% \rightarrow 0\%$ with end-stop zero-homing) to prevent calcification and pump/valve seizure.
  * **Return & Non-Persistence:** On completion, the controller returns to `MODE_SUMMER`. Transient modes (`MODE_ANTI_SEIZE`, `MODE_TEST_OVERRIDE`) are runtime-only and never written to NVS.

---

## 3. Test Override Mode & Direct Mixer Jogging (`MODE_TEST_OVERRIDE`)

### 3.1 Purpose & Authority

Initiated **only by a human user via the local Web Dashboard** for:
1. Bench and hardware integration testing,
2. Temporary manual heating operation (bounded by `test_mode_max_s`, e.g., $7200\text{ s} = 2\text{ hours}$) if HA or WiFi is offline, or
3. Executing structured system-identification routines requested by the Observer.

### 3.2 Pipeline Contract & Expiry Transition

* **Pipeline Intake:** Test mode acts as a command emulator entering at the same L4 intake point as HA commands, remaining **fully subject to all L1 safety rules** (`t_max_hard`, `t_max_warn`, `grad_limit`, sensor faults, and `boiler_min_off_ms`), with a visible countdown timer (`test_mode_rem_s`).
* **Direct Mixer Jogging:** Specifically in `MODE_TEST_OVERRIDE`, L2 closed-loop mixer control can be paused to allow **direct manual mixer jogging** from the Web Dashboard (issuing small open/close pulses or step targets directly through L1 $\rightarrow$ L0) to verify mechanical travel and wiring polarity.
* **Test Mode Expiry Mid-Heating (HA Offline):** When `test_mode_rem_s` reaches $0$, mode transitions to the persisted NVS mode (normally `MODE_HEAT`). If HA is offline, `keepalive_age_s >= demand_timeout_s` is immediately true, which funnels safely into the staged shutdown: `heater_phys` is opened immediately (`DIV_DEMAND_LOSS`), running pumps circulate for `pump_stop_after_heating_s`, and then all pumps turn off (`DIV_AFTER_HEATING_TIMEOUT`).

### 3.3 Built-In Identification Test Routines (User-Initiated, Observer-Consumed)

Because the Observer has **zero command authority** and cannot trigger relays or tests directly, it may flag a request in the logs/UI asking the human user to launch one of three bounded identification routines from the Web Dashboard's Test Mode menu:

1. **Test 1 — Valve Stroke & Encoder Calibration Sweep:**
   * **Precondition:** `heater_phys = OFF`.
   * **Sequence:** Drives the mixer to the closed end-stop ($1.2 \times \text{travel\_time\_ms}$) to zero-home, then steps through $0\% \rightarrow 25\% \rightarrow 50\% \rightarrow 75\% \rightarrow 100\% \rightarrow 0\%$.
   * **Output:** Logs dead-reckoned position (`pos_est_dr`) vs. encoder position (`pos_encoder`) at each step to calibrate `travel_time_ms`, mechanical hysteresis, and valve characteristic.
2. **Test 2 — Floor Dead-Time & Step-Response Identification:**
   * **Precondition:** `pump_floor = ON`, `heater = ON`, and $T_{\text{boiler}}$ hot.
   * **Sequence:** Holds the mixer at a safe baseline position until $T_{\text{mix\_out}}$ and $T_{\text{return\_floor}}$ stabilize, then executes a bounded step sequence (e.g., $+10\%$, hold $3\text{ min}$, $-10\%$, hold $3\text{ min}$, strictly bounded by `t_max_warn`). If $T_{\text{mix\_out}} \ge t_{\text{max\_warn}}$, the routine immediately aborts, commands the mixer to $0.0$, and logs `TEST_ABORTED_OVERTEMP`.
   * **Output:** Provides clean excitation telemetry in the event/slow logs for the Observer to compute `delay_s`, `gain`, `ff_boiler`, `ff_return`, `kp`, and `ki`.
3. **Test 3 — Multi-Circuit Radiator Coupling Probe:**
   * **Precondition:** Floor circuit running at steady state.
   * **Sequence:** Toggles `pump_rad1` and `pump_rad2` `ON` for a bounded window while recording $T_{\text{boiler}}$ and $T_{\text{mix\_out}}$.
   * **Output:** Allows the Observer to quantify boiler supply temperature droop and hydraulic coupling to tune `rad_coupling_ff`.