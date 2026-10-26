# Floor Heating Plant Simulation & System Identification Suite

This folder contains the complete System Identification (SysID) extraction pipeline, thermodynamic/hydraulic plant model, and Hardware-In-The-Loop (HIL) simulator for evaluating the ESP32 heating controller.

---

## 1. Directory Structure

| File | Purpose |
| :--- | :--- |
| [`sysid_all.py`](sysid_all.py) | Master extraction script; runs all 3 subsystem identifications on DuckDB and generates `identified_parameters.json`. |
| [`sysid_heater.py`](sysid_heater.py) | Boiler/heater supply dynamics identification (PID sine oscillations, warmup/cooldown rates). |
| [`sysid_mixer.py`](sysid_mixer.py) | 3-way mixer valve blending dynamics, transport delay $L_{\text{mix}}$, and empirical valve curve $x(pos)$. |
| [`sysid_floor.py`](sysid_floor.py) | Underfloor hydronic loop delay $L_{\text{floor}}$, slab thermal inertia $\tau_{\text{floor}}$, and $\Delta T$ distributions. |
| [`sysid_report.md`](sysid_report.md) | In-depth engineering report documenting mathematical formulations, regressions, and parameter bounds. |
| [`identified_parameters.json`](identified_parameters.json) | Clean JSON parameter file used directly by the simulator. |
| [`floor_model.py`](floor_model.py) | Physical ODE and hydraulic plant model (Heater + Mixer + Floor Loop). |
| [`simulator.py`](simulator.py) | Hardware-In-The-Loop simulator communicating via HTTP REST with the ESP32 emulator (also supports `--mock`). |
| [`plot_run.py`](plot_run.py) | Generates multi-panel graphical plots (`.png`) and computes quantitative performance metrics (rise time, overshoot, ripple, motor duty cycle). |

---

## 2. Identified Physical Parameters

Derived from historical active heating season telemetry (`heatpumps.duckdb`):

```json
{
  "boiler": {
    "nominal_temp_c": 67.9,
    "incoming_nominal_c": 65.02,
    "oscillation_period_s": 3600.0,
    "oscillation_amplitude_k": 3.5,
    "warmup_rate_k_per_s": 0.05,
    "cooldown_rate_k_per_s": -0.0469
  },
  "mixer": {
    "dead_time_s": 120.0,
    "tau_mix_s": 30.0,
    "travel_time_ms": 145000,
    "valve_curve_c1": 1.1354,
    "valve_curve_c2": -1.0844
  },
  "floor": {
    "loop_delay_s": 30.0,
    "nominal_return_temp_c": 25.19,
    "steady_state_delta_t_k": 8.81,
    "min_useful_delta_t_k": 2.0,
    "tau_floor_s": 2400.0,
    "return_rise_rate_k_per_s": 0.00501,
    "cooldown_rate_k_per_s": -0.002
  }
}
```

---

## 3. Running Hardware-in-the-Loop (HIL) Simulation

### Prerequisites
1. Remote ESP32 device running `pio remote agent`.
2. Firmware built and uploaded with emulation enabled (`esp32dev_sim` environment):
   ```bash
   pio remote run --target upload
   ```
3. Verify serial boot log with enforced timeout:
   ```bash
   timeout 10s pio remote device monitor -b 115200 || true
   ```
4. Confirm emulator HTTP interface is reachable (e.g. at `http://192.168.2.14` or `http://localhost:9090`).

### Starting the HIL Simulator
Run the simulator pointing to your ESP32 IP address or forwarded tunnel port:
```bash
# Real-time HIL simulation for 30 minutes (1800 s)
python3 simulation/simulator.py --url http://192.168.2.14 --duration-s 1800

# Or with custom timestep and output file:
python3 simulation/simulator.py --url http://localhost:9090 --step-s 1.0 --duration-s 3600 --output simulation/hil_run.csv
```

The simulator will:
* Poll `/api/sim` to read controller actuation (relay states, mixer motor energization, direction, DR position).
* Step the thermodynamic floor ODEs forward by $\Delta t$.
* Push simulated temperatures ($T_{\text{boiler}}$, $T_{\text{forward}}$, $T_{\text{return}}$) back to `/api/sim/temperature`.
* Keep simulated HA demand intent alive via `/api/sim/intent`.
* Stream telemetry to console and write high-resolution CSV records.

---

## 4. Running in Mock Mode (Offline Testing)

If the physical board or remote runner is not connected, test the closed-loop dynamics immediately using the built-in controller clone:
```bash
# 30-minute fast simulation (takes ~3 seconds)
python3 simulation/simulator.py --mock --fast --duration-s 1800 --output simulation/test_run_mock.csv

# Cold start test (floor starts at 20 °C)
python3 simulation/simulator.py --mock --fast --cold --duration-s 2400 --output simulation/test_run_cold.csv
```

---

## 5. Visualizing Telemetry & Performance Metrics

Use `plot_run.py` to analyze any CSV log file:
```bash
python3 simulation/plot_run.py simulation/test_run_mock.csv
```

Outputs:
1. Console performance summary:
   * **Rise Time:** Time to enter the $\pm 2\text{ K}$ deadband.
   * **Peak Forward Temperature & Overshoot:** Max temperature excursion above setpoint.
   * **Steady-State Error & Ripple:** Mean offset and peak-to-peak oscillation.
   * **Mixer Motor Duty Cycle & Move Count:** Measures actuator wear and hunting.
2. PNG graphical plot:
   * Panel 1: $T_{\text{boiler}}$, $T_{\text{incoming}}$, $T_{\text{forward}}$, $T_{\text{return}}$, setpoint shadow, and floor $\Delta T$.
   * Panel 2: True valve position vs controller Dead Reckoning vs controller target.
   * Panel 3: Motor energization direction (Open / Close / Idle) and pump relay states.
