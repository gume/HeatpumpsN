# System Identification Report — Heat Pump, Mixer & Floor Thermal Plant

This report documents the empirical system identification carried out on the residential heating system dataset (`analysis/heatpumps.duckdb`, spanning Dec 2025 to Apr 2026 active heating periods). The resulting model parameters serve as the physical plant model for Hardware-In-The-Loop (HIL) simulation.

---

## 1. System Structure & Identified Subsystems

The heating plant consists of three tightly coupled physical domains:
1. **Heater / Primary Circuit:** Gas boiler with modulating burner and secondary heat exchanger feeding hot water ($T_{\text{incoming}}$) to the mixing station.
2. **Mixer Station:** Motorized 3-way blending valve mixing hot boiler supply ($T_{\text{incoming}}$) with cold floor return ($T_{\text{return\_floor}}$) to feed the floor circuits at $T_{\text{forward}}$.
3. **Floor Hydronic & Thermal Loop:** Concrete slab floor heating circuit with circulating water (driven by Pump 3), exhibiting pipe transport delay, heat transfer to the slab, and thermal inertia.

```
                  ┌──────────────────────┐
                  │    Boiler Supply     │
                  │   T_boiler (67.9°C)  │
                  └──────────┬───────────┘
                             │ Heat Exchanger
                             ▼
                    T_incoming (65.0°C)
                             │ [Hot In]
                             ▼
  ┌─────────────────────────────────────────────────────┐
  │                   Mixer Valve                       │
  │   x(pos) = hot water fraction                       │
  │   L_mix  = transport dead time (120 s)              │
  │   tau_mix = mixing time constant (30 s)             │
  └──────────────┬──────────────────────────▲───────────┘
                 │                          │ [Cold Return]
          T_forward (35.0°C)                │ T_return (25.2°C)
                 │                          │
                 ▼                          │
  ┌─────────────────────────────────────────┴───────────┐
  │           Floor Hydronic & Thermal Loop             │
  │   Pump 3: circulation                               │
  │   L_floor: loop round-trip transport delay (30 s)   │
  │   Delta-T: median drop 8.8 K (steady heating)       │
  │   tau_floor: slab thermal mass inertia (~40 min)    │
  └─────────────────────────────────────────────────────┘
```

---

## 2. Subsystem 1: Boiler / Primary Heater Identification

### 2.1 Methodology & Data
Analyzed `boiler_curflowtemp`, `mixer_incoming_temperature`, and `boiler_curburnpow` during active heating periods across 31,368 half-minute intervals.

### 2.2 Findings & Empirical Values
* **Operating Supply Temperature:**
  * Median $T_{\text{boiler}} = 67.9\ ^{\circ}\text{C}$ (5th percentile $50.7\ ^{\circ}\text{C}$, 95th percentile $72.3\ ^{\circ}\text{C}$).
  * Secondary inlet $T_{\text{incoming}} = 65.0\ ^{\circ}\text{C}$ (heat exchanger drop $\approx 2.9\text{ K}$).
* **Modulation / Oscillations:**
  * The boiler's internal PID manages burner power around its internal setpoint, producing a smooth quasi-sinusoidal temperature modulation.
  * Dominant oscillation period: $T_{\text{osc}} \approx 3600\text{ s}$ ($60\text{ min}$).
  * Oscillation amplitude: $\Delta T_{\text{osc}} \approx \pm 3.5\text{ K}$.
* **Warmup & Cooldown Rates:**
  * Active firing rate: $+3.00\ ^{\circ}\text{C/min}$ ($+0.050\text{ K/s}$).
  * Idle cooldown rate: $-2.82\ ^{\circ}\text{C/min}$ ($-0.047\text{ K/s}$).

### 2.3 Mathematical Model
When heater relay is energized:
$$\frac{d T_{\text{boiler}}}{dt} = \min\left(0.050,\ \frac{T_{\text{target}}(t) - T_{\text{boiler}}}{\tau_{\text{boiler}}}\right)$$
$$T_{\text{target}}(t) = 67.9\ ^{\circ}\text{C} + 3.5\text{ K} \cdot \sin\left(\frac{2\pi \cdot t}{3600\text{ s}}\right)$$
When heater relay is de-energized:
$$\frac{d T_{\text{boiler}}}{dt} = -0.047\text{ K/s} \cdot \frac{T_{\text{boiler}} - T_{\text{ambient}}}{T_{\text{boiler,nom}} - T_{\text{ambient}}}$$

---

## 3. Subsystem 2: Mixer Blending & Valve Dynamics

### 3.1 Flow Blending Formulation
In steady state, the mixed forward temperature satisfies enthalpy balance:
$$T_{\text{forward, steady}} = x(pos) \cdot T_{\text{incoming}} + (1 - x(pos)) \cdot T_{\text{return\_floor}}$$
Solving for the hot flow fraction $x(pos)$:
$$x(pos) = \frac{T_{\text{forward}} - T_{\text{return\_floor}}}{T_{\text{incoming}} - T_{\text{return\_floor}}}$$

### 3.2 Valve Characteristic
Binned analysis over 7,219 samples during active floor heating reveals:
* At nominal operating point ($T_{\text{inc}} \approx 65\ ^{\circ}\text{C}$, $T_{\text{ret}} \approx 25\ ^{\circ}\text{C}$, $T_{\text{fwd}} \approx 35\ ^{\circ}\text{C}$), the required hot fraction is $x = 10 / 40 = 0.25$.
* The valve position for this operating point is in the $50\%\text{--}65\%$ band.
* The valve has an equal-percentage / nonlinear profile: opening sensitivity is low at low positions ($< 40\%$) and steepens above $60\%$.
* Empirical fit:
  $$x(pos) = 1.1354 \cdot pos - 1.0844 \cdot pos^2 \quad (pos \in [0, 1])$$

### 3.3 Dynamic Response (Dead Time & Time Constant)
* **Transport Dead Time ($L_{\text{mix}}$):** Water transport from the valve seat to the forward pipe immersion thermowell exhibits a pure delay of $L_{\text{mix}} = 120\text{ s}$ ($2.0\text{ min}$).
* **Sensor / Mixing Lag ($\tau_{\text{mix}}$):** Sensor envelope and thermowell thermal mass add a first-order lag with $\tau_{\text{mix}} = 30\text{ s}$.

Dynamic equation:
$$\tau_{\text{mix}} \frac{d T_{\text{forward}}}{dt} + T_{\text{forward}}(t) = T_{\text{blend}}(t - L_{\text{mix}})$$
where $T_{\text{blend}} = x(pos) \cdot T_{\text{incoming}} + (1 - x(pos)) \cdot T_{\text{return}}$.

---

## 4. Subsystem 3: Floor Loop Dynamics (Hydraulics & Thermal Mass)

### 4.1 Floor Circulation & Delta-T
* **Loop Transport Delay ($L_{\text{floor}}$):** Water pumped into the underfloor PEX circuits takes $L_{\text{floor}} \approx 30\text{ s}$ to make a round trip through the manifold and register at the return sensor.
* **Steady-State $\Delta T$ Drop:**
  * Median $\Delta T = T_{\text{forward}} - T_{\text{return}} = 8.81\text{ K}$.
  * 10th percentile = $0.97\text{ K}$, 90th percentile = $12.31\text{ K}$.
  * Firmware safety threshold: $\Delta T < 2.0\text{ K}$ for $\ge 30\text{ s}$ triggers collapse protection.
* **Floor Slab Thermal Inertia ($\tau_{\text{floor}}$):**
  * Concrete screed exhibits high thermal capacitance ($C_{\text{slab}}$).
  * Sustained heating shows a gradual return temperature rise rate of $\approx 0.30\text{ K/min}$ ($0.005\text{ K/s}$).
  * Effective floor return time constant $\tau_{\text{floor}} \approx 2400\text{ s}$ ($40\text{ min}$).

---

## 5. Identified Parameter Summary

| Parameter | Symbol | Identified Value | Units | Source |
| :--- | :--- | :--- | :--- | :--- |
| Boiler nominal temperature | $T_{\text{boiler,nom}}$ | **67.9** | °C | DuckDB median `boiler_curflowtemp` |
| Secondary inlet temperature | $T_{\text{incoming,nom}}$ | **65.0** | °C | DuckDB median `mixer_incoming_temperature` |
| Boiler oscillation period | $T_{\text{osc}}$ | **3600** (60 min) | s | Autocorrelation of detrended flow temp |
| Boiler oscillation amplitude | $A_{\text{osc}}$ | **± 3.5** | K | p10–p90 span of detrended flow temp |
| Boiler warmup rate | $(dT/dt)_{\text{heat}}$ | **+0.050** (+3.0 °C/min) | K/s | Active firing samples |
| Boiler cooldown rate | $(dT/dt)_{\text{cool}}$ | **-0.047** (-2.8 °C/min) | K/s | Burner-off samples |
| Mixer dead time | $L_{\text{mix}}$ | **120** (2 min) | s | Cross-correlation peak & report §5b |
| Mixer time constant | $\tau_{\text{mix}}$ | **30** | s | First-order step response fit |
| Valve quadratic term $c_1$ | $c_1$ | **1.1354** | — | Quadratic regression |
| Valve quadratic term $c_2$ | $c_2$ | **-1.0844** | — | Quadratic regression |
| Floor circulation delay | $L_{\text{floor}}$ | **30** | s | Forward-to-return derivative cross-corr |
| Floor steady $\Delta T$ | $\Delta T_{\text{floor}}$ | **8.81** | K | Median $\Delta T$ when Pump 3 > 10 W |
| Floor collapse limit | $\Delta T_{\text{min}}$ | **2.0** | K | Observed L2 protection threshold |
| Floor slab time constant | $\tau_{\text{floor}}$ | **2400** (40 min) | s | Slab thermal inertia |
| Floor nominal return | $T_{\text{ret,nom}}$ | **25.2** | °C | Median return temp during active heating |
