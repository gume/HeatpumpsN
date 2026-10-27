# Parameter Extraction & System Identification Plan
**Project:** ESP32 Floor Heating Hardware-in-the-Loop (HIL) Simulator  
**Location:** `/home/gume/HeatpumpsN/simulation/`  
**Document Version:** 2.0  
**Status:** Ready for Phase 1 Execution  

---

## 1. Project Goal & Overview

The objective of this parameter extraction process is to perform a clean-slate, bottom-up System Identification of the hydronic heating plant using historical telemetry stored in `/home/gume/HeatpumpsN/analysis/heatpumps.duckdb`.

The identified parameters will form the mathematical foundation of a deterministic, high-fidelity **Hardware-in-the-Loop (HIL) Floor Heating Simulator**. The simulator will enable testing, tuning, and validation of new ESP32 firmware control algorithms (replacing the legacy controller) before deployment on physical hardware.

---

## 2. Plant Physical Architecture & Operational Domain Knowledge

### 2.1 Hydraulic Plant Layout
* **Primary Heat Source (Boiler):**
  * Gas boiler with its own autonomous internal modulation and safety cutoff logic.
  * Operating temperatures: produces $\sim 80^\circ\text{C}$ supply during Domestic Hot Water (DHW) charging and $\sim 70^\circ\text{C}$ supply during space heating.
  * Internal throttling: The boiler may shut down or modulate down independently even if space heating demand is active, predominantly when heat cannot be dissipated into the loops (low hydraulic flow / high return temperature).
* **DHW Priority Mechanism:**
  * DHW charging (`binary_sensor.boiler_wwcharging`) takes absolute priority.
  * During DHW charging, all heated primary water is diverted away from the heating heat exchanger to the DHW cylinder. The heating heat exchanger receives zero heat.
* **Primary Heat Exchanger (HX) & Supply Fork:**
  * Primary loop from boiler through the plate heat exchanger is very short.
  * Immediately downstream of the secondary side of the heat exchanger is a hydraulic fork splitting into:
    1. **Radiator circuits:** Circulated by Pump 1 (`heatpumps_pump_1_power`) and Pump 2 (`heatpumps_pump_2_power`).
    2. **Underfloor heating (UFH) circuit:** Fed via a 3-way motorized mixing valve and circulated by Pump 3 (`heatpumps_pump_3_power`).
  * `mixer_incoming_temperature` ($T_{\text{incoming}}$) is physically located right at this fork.
* **Underfloor Heating (UFH) Loop:**
  * Total heated floor area is $\sim 70\,\text{m}^2$, divided into multiple parallel circuits across several rooms.
  * Supply and return water temperatures are measured at the central manifold:
    * `mixer_forward_temperature` ($T_{\text{forward}}$): Supply entering floor circuits after mixing.
    * `mixer_return_temperature` ($T_{\text{return}}$): Return collection manifold before re-entering mixer valve.

### 2.2 Sensor Characteristics & Fault Signatures
* **Temperature Sensors (Dallas DS18B20 1-Wire):**
  * Configured with a reporting threshold deadband (`delta: 0.1^\circ\text{C}`) and throttle (`60s`).
  * **Critical Rule:** If a temperature sensor reading is missing or unchanged across consecutive sample intervals, the sensor is healthy and operational—the physical temperature change simply did not exceed the reporting delta. Forward-fill / sample-and-hold is physically valid.
* **Mixer Valve Position Tracking (Dual Complementary Sensors):**
  1. **Internal Actuator Timing Dead-Reckoning (`mixer_state`):**
     * Calculated in firmware by accumulating open/close relay pulse runtimes.
     * Prone to integration drift over extended operation.
     * Periodic re-zeroing: A reset routine drives the valve fully closed to $0\%$ (typically nightly or occasionally) to re-establish the zero baseline.
  2. **Rotary Knob Accelerometer (`mixermeter_bmi160_mixer_value`):**
     * Measures mechanical knob tilt angle directly via a BMI160 accelerometer.
     * **Fault Mode:** Randomly freezes and holds a static constant value until reset.
     * **Freeze Detection Criterion:** If `mixer_state` indicates movement while `mixermeter_bmi160_mixer_value` remains stationary, the accelerometer is frozen.
     * **Sensor Fusion Strategy:** Use `mixer_state` to detect accelerometer freezes; use valid, unfrozen accelerometer readings (especially following re-zero events) to calibrate actuator slew rate and correct dead-reckoning drift.
* **Circulation Pump Power Meters:**
  * `heatpumps_pump_1_power`, `heatpumps_pump_2_power` (Radiators), `heatpumps_pump_3_power` (Floor).
  * Wilo variable-speed circulators operated at static speed presets (rare manual speed adjustments during winter).
  * Meters are coarsely calibrated; their primary utility is as **binary state indicators** (PUMP_ON vs. PUMP_OFF).
* **Mixer Power Meter:**
  * Shows erroneous/garbage readings if present in data. **Must be completely ignored.**
* **Room Temperature Sensor (`temperature_humidity_sensor_d281_temperature`):**
  * Located in a room heated exclusively by underfloor heating (no radiators).
  * Positioned near a window: highly susceptible to direct solar radiation gain on sunny days, introducing significant high-frequency thermal bias. Solar bias must be isolated when estimating room thermal loss.

### 2.3 Legacy Controller Firmware Artifacts
* The historical firmware utilized a PID controller with fixed travel limits (`mixer_minimum`, `mixer_maximum`) and failsafe guards.
* **Failsafe Emergency Closures:** A frequent failure mode occurred when radiator pumps switched states, causing sudden hydraulic pressure/temperature transients that tripped safety rules. The firmware responded by slamming the mixer valve to $0\%$ (fully closed) or to minimum limit. These abrupt drops are firmware artifacts, not natural thermal dynamics.
* **Scheduling Intent:** While the legacy system attempted time-of-day segregation to avoid concurrent radiator and floor heating, the target design for the new controller is cooperative concurrent operation.

---

## 3. Strict Rules of Engagement & Methodology

To ensure total reproducibility, mathematical rigor, and engineering reliability:

1. **Phase-by-Phase Gating:**
   * Work proceeds strictly one step at a time.
   * **Do not advance to the next step until all findings, queries, and outputs of the current step are reviewed and accepted.**
2. **Directory Isolation:**
   * **All execution scripts, data dumps, plots, and markdown reports must be written exclusively inside `/home/gume/HeatpumpsN/simulation/`.**
   * No files outside `simulation/` may be created or modified. Read-only access to `/home/gume/HeatpumpsN/analysis/` and `/home/gume/HeatpumpsN/firmware/` is allowed.
3. **Standalone Versioning:**
   * Every script and report must carry an explicit step and version tag (e.g., `step1_data_audit_v1.py`, `step1_data_audit_report_v1.md`).
   * When iterating, create a new version (e.g., `v2`).
   * **New versions must be 100% self-contained and standalone.** Do not require the reader to cross-reference previous versions.
4. **Independent Verification:**
   * Any figures, formulas, or inferences derived from prior notes in `/home/gume/HeatpumpsN/analysis/` must be independently verified against the raw DuckDB database. Unverified prior claims cannot be assumed true.
5. **Full Reproducibility:**
   * Every report must document the exact script invocation, runtime parameters, data filtering logic, and mathematical formulations so that any result can be re-derived identically.

---

## 4. 6-Phase Master System Identification Plan

```mermaid
flowchart TD
    P1["Phase 1: Database Audit, Sensor Quality & Regime Segmentation"] --> P2["Phase 2: Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)"]
    P2 --> P3["Phase 3: 3-Way Mixer Static Mixing Characteristics"]
    P3 --> P4["Phase 4: Mixer Actuator Slew, Delay & Thermowell Dynamics"]
    P4 --> P5["Phase 5: Underfloor Slab & Hydronic Transport Dynamics"]
    P5 --> P6["Phase 6: Standalone HIL Simulator Assembly & Closed-Loop Validation"]
```

### Phase 1: Database Audit, Sensor Quality & Regime Segmentation
* **Objective:** Comprehensive schema inspection of `analysis/heatpumps.duckdb`, measurement intervals, threshold deadband effects, dual mixer sensor cross-validation, and operating regime segmentation.
* **Deliverables:** `step1_data_audit_v1.py`, `step1_data_audit_report_v1.md`.

### Phase 2: Primary Heat Exchanger Dynamics ($T_{\text{boiler}} \to T_{\text{incoming}}$)
* **Objective:** Model the heat transfer from boiler supply water through the plate heat exchanger to the common manifold fork.
* **Key Tasks:** Isolate space heating periods from DHW charging; quantify radiator pump cross-coupling effects on $T_{\text{incoming}}$ flow rate and temperature droop; establish the upstream thermal boundary condition for the mixer.

### Phase 3: 3-Way Mixer Valve Static Characteristic
* **Objective:** Determine the static mixing law relating valve position, incoming supply temperature, and return loop temperature to the mixed forward temperature:
  $$T_{\text{forward, steady}} = f(pos, T_{\text{incoming}}, T_{\text{return}})$$
* **Key Tasks:** Characterize valve hydraulic authority, non-linear flow geometry, leak-by at $0\%$, and heat exchanger balance during steady pump runs.

### Phase 4: Mixer Actuator & Sensor Dynamics ($pos \to T_{\text{forward}}(t)$)
* **Objective:** Quantify dynamic lag between actuator commands and observed forward temperature response.
* **Key Tasks:** Actuator motor slew rate ($\text{s}/100\%$), mechanical backlash/deadband, hydraulic transport delay from valve to sensor, and Dallas DS18B20 thermowell thermal time constant ($\tau_{\text{sensor}}$).

### Phase 5: Underfloor Slab & Hydronic Transport Dynamics ($T_{\text{forward}} \to T_{\text{return}}(t)$)
* **Objective:** Model the dynamic thermal response of the underfloor heating slab and piping circuits.
* **Key Tasks:** Transport delay estimation ($\tau_{\text{transport}}$ through the 70 m² pipe networks); lumped-parameter thermal capacitance ($C_{\text{slab}}$) and conductance ($R_{\text{slab}\to\text{room}}$); room thermal losses with window solar radiation bias rejection.

### Phase 6: Standalone HIL Simulator Assembly & Validation
* **Objective:** Integrate identified subsystems into a deterministic state-space simulator in Python / C++ compatible with the ESP32 firmware build system.
* **Key Tasks:** Validate open-loop and closed-loop performance against long historical telemetry continuous segments.

---

## 5. Specification for Step 1 Execution

When triggered, the Step 1 script (`step1_data_audit_v1.py`) will perform the following exhaustive audit:

1. **Schema & Coverage Analysis:**
   * Query DuckDB for all tables, views, and row counts.
   * Inspect timestamps: total duration, start/end dates, row counts per entity.
   * Distribution of time deltas ($\Delta t$): quantify reporting intervals, jitter, and Dallas sensor threshold deadband effects ($10\,\text{s}$ base, up to $60\,\text{s}$ throttle).
2. **Sensor Quality, Quantization & Health:**
   * Summary statistics: Min, Max, P5, P50, P95, Mean, StdDev, and NaN/NULL ratios for all primary entities.
   * Empirical quantization step: smallest observed non-zero difference between consecutive readings.
   * Mixer position sensor comparison: Cross-correlation between `mixer_state` and `mixermeter_bmi160_mixer_value`, identification of accelerometer freeze periods and nightly reset events.
3. **Operational Regime Segmentation:**
   * Categorize telemetry into distinct operating regimes based on pump powers and DHW status:
     * **Regime A (Floor Heating Only):** Floor Pump (Pump 3) ON, Radiator Pumps (1 & 2) OFF, DHW OFF.
     * **Regime B (Combined Heating):** Floor Pump ON, at least one Radiator Pump ON, DHW OFF.
     * **Regime C (Radiators Only):** Floor Pump OFF, Radiator Pumps ON, DHW OFF.
     * **Regime D (DHW Priority Charging):** DHW Active (`boiler_wwcharging` = 1).
     * **Regime E (Idle / Standby):** All pumps OFF, burner OFF, DHW OFF.
   * Calculate cumulative hours and identify continuous segments ($> 15\,\text{min}$) suitable for dynamic parameter identification.
4. **Deliverables:**
   * Standalone script: `/home/gume/HeatpumpsN/simulation/step1_data_audit_v1.py`
   * Standalone report: `/home/gume/HeatpumpsN/simulation/step1_data_audit_report_v1.md`
