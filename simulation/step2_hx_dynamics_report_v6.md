# Phase 2 — Version 6.1: Primary Heat Exchanger Dynamics (T_boiler → T_incoming)

**Project:** Hardware-in-the-Loop (HIL) Floor Heating Simulator
**Version:** 6.1 (engineering sign-off)
**Generated:** 2026-10-28 19:32:21
**Script:** `simulation/step2_hx_dynamics_v6.py`
**Database:** `analysis/heatpumps.duckdb` (read-only)
**Generated C++:** `simulation/step2_hx_dynamics_v6.hpp`

---

## 0. Sign-off summary

Version 6.1 is the Phase 2 engineering sign-off release. It fixes the
low-lift/pump-off coupling,
adds a cold-start transport front, removes all backward fills and hard-coded
paths, enforces a strict FIT/TUNE/VAL split, and makes the Python and C++
implementations numerically identical.

| Gate | Result |
|---|---|
| Low-lift decoupled from pump-off hold | Branch 2A (pump OFF) and 2B (pump ON, `t_boiler <= t_return + 1.0`) are separate |
| Single-step firing glitch | Fixed (`consec_bp == 1` → `max(tb_active, t_boiler)`; `consec_bp >= 2` → hard sync + timer clear) |
| Cold-start dead time | `L_cold_start = 120.0 s`, `tau_cold_front = 5.0 s`, fitted on TUNE-1 only |
| Python/C++ parity | `g++ -O2 -std=c++17`, 15,769 rows, max |Δ| = `4.999e-11 °C` (< 1e-4) |
| Five synthetic unit tests | 5/5 passed |
| Strict split | FIT (1–24 Dec, FIT-1, FIT-DHW) / TUNE (TUNE-1, TUNE-2) / VAL (VAL-1..4, VAL-7 untouched + new VAL-5, VAL-6) |
| Backward fill | Zero `.bfill()`; 30-min warm-up + ASOF join; evaluation-boundary re-anchor; NaN asserted away |
| Staleness | `tb_age_s`, `bp_age_s`, `pmod_age_s` guard holds state on gateway drops |
| Portable paths | `BASE_DIR = Path(__file__).resolve().parent.parent`; `HEATPUMPS_DB_PATH` / `HEATPUMPS_OUT_DIR` overrides |

**What is *not* claimed.** Residuals remain. The v6.1 final-shutdown RMSE on
TUNE-1 is `1.771 °C` (v5: `1.748 °C`),
and VAL-5's post-pump-off idle cooling residual is material. Section 6 and
Section 9 quantify these residuals rather than claiming that they are
eliminated.

### 0.1 v6.1 HIL audit corrections

| # | Defect | Correction |
|---|---|---|
| 1 | Measured `T_incoming` was a `step()` input, so HIL/Step 3 would need the simulated output | Removed `tinc` from Python and C++ `step()`; the cold-start trigger and ring initialisation use internal `sim` / `sim_` |
| 2 | Cold-start plug-flow ring advanced with the primary pump off or during DHW priority | Ring, `t_front` and `cold_elapsed` advance only when `pmod > 0.0 && !ww` |
| 3 | Re-anchor skipped sample 180, wiped an active cold front and leaked warm-up branch counts | Re-anchor then still call `step()`; preserve an active `cold_on` ring; clear `branch_counts` at the boundary |
| 4 | `tau_ambient = 36000 s` under-cooled a hot manifold (VAL-5 idle) | FIT-only hot-manifold idle identification (`~p1 & ~p2 & ~p3 & ~ww`, `T_inc - T_room > 10 °C`) gives `tau_idle` |
| 5 | `detect_shutdown_windows()` labelled idle/DHW episodes as cold/cutoff/final shutdowns | Detection requires median-filtered secondary space-heating circulation (`p_any & ~ww`); VAL-7 now reports no windows |
| 6 | `"ABCDEother"` iterated character-by-character | Tuple keys `("A", "B", "C", "D", "E", "other")` |
| 7 | C++ parity skipped UT1–UT5 | The same five synthetic frames feed both the Python tests and the parity CSV |
| 8 | JSON contained a machine-specific `/home/...` database path | JSON writes the database path relative to `BASE_DIR` |

---

## 1. Scope and strict three-way split

The v5 report reused the old VAL-5 and VAL-6 episodes as validation after
using them for tuning. That is leakage. v6 fixes the split as follows.

| Partition | Episodes | Window | Used for |
|---|---|---|---|
| **FIT** | FIT window | 2025-12-01 22:00 → 2025-12-24 23:59 | Regime effectiveness ratios, `tau_rad_off` |
| **FIT** | FIT-1 | 2026-01-14 00:00 → 05:00 | `tau_hx`, `eps_A` |
| **FIT** | FIT-DHW | 2025-12-03 08:18 → 08:45 | `dhw_delay_s`, `tau_dhw_fast` |
| **TUNE** | TUNE-1 (old VAL-5) | 2025-12-27 16:06 → 20:58 | Cold start, cutoffs, final shutdown |
| **TUNE** | TUNE-2 (old VAL-6) | 2026-01-01 14:50 → 15:25 | DHW divert and return |
| **VAL** | VAL-1..VAL-4, VAL-7 | unchanged from v5 | Untouched validation |
| **VAL** | VAL-5 (new) | 2026-02-08 04:27 → 10:11 | Untouched: cold start + mid-run cutoffs + final shutdown |
| **VAL** | VAL-6 (new) | 2026-02-18 05:50 → 06:55 | Untouched: circulating DHW `ww 1→0` with secondary pumps running |

TUNE and VAL metrics are reported separately in Sections 4 and 5.

---

## 2. Model

### 2.1 HX outlet

The plate heat exchanger is a first-order lag toward the secondary-return
referenced target:

```
T_hx[k] = (1 - a) * T_hx[k-1] + a * (T_return[k] + eps * (T_boiler,eff[k] - T_return[k]))
a = DT / (tau_hx + DT)
```

`eps` depends on the secondary regime: `eps_A` (floor only), `eps_B1`
(combined, one radiator), `eps_B2` (combined, two radiators), `eps_C`
(radiators only).  Pump states are debounced with a strictly causal trailing
majority (`2 * ones >= count`, window 31) so no future samples are used.

### 2.2 Branch table (v6)

| Branch | Condition | Update |
|---|---|---|
| Active heating | secondary pump on, burner firing | first-order toward `T_return + eps*(T_boiler - T_return)` |
| **2A Pump-off hold** | primary pump OFF, `pump_off_timer < L_pump_off` | hold previous state |
| **2A Pump-off plunge** | primary pump OFF, timer elapsed, floor pump ON | first-order toward `T_return` with `tau_cutoff` |
| **2A Radiator stagnation** | primary pump OFF, only radiator pumps ON | first-order toward `T_return` with `tau_rad_off` (FIT-identified) |
| **2B Low lift** | primary pump ON and `T_boiler <= T_return + 1.0` | **immediate** first-order toward `T_return` with `tau_cutoff`; no hold |
| Burner-off casing | primary pump ON, burner OFF, usable lift | casing `tb_active` decays with `tau_burn_off` after `L_burn_off` |
| DHW divert | `ww = 1` and any secondary pump | hold `dhw_delay_s`, then first-order toward `T_return` with `tau_dhw_fast` |
| Post-DHW return | `ww = 0` and `post_dhw_timer < post_dhw_delay_s` | first-order toward `T_return` with `tau_cutoff` |
| Idle | no secondary pump | first-order toward `T_room` with `tau_idle` (FIT-identified) |

The low-lift branch is checked **after** the pump-off branch and **before** the
casing branch. It therefore never inherits the `L_pump_off` hold. This is the
defect that produced the v5 early-drop spike.

### 2.3 Cold-start transport front

A cold start is latched when all of the following hold at a firing/pump-start
event:

```
idle_timer >= L_idle_trigger (1800 s)
T_hx (internal simulated outlet) < 28 °C
T_boiler - T_hx > 15 °C
```

The primary pipe is represented by a plug-flow ring buffer of length
`N = round(L_cold_start / DT)` initialised with the cold internal outlet
temperature, followed by a first-order dispersion `tau_cold_front`. The
delayed/dispersed boiler temperature replaces `T_boiler` in the active and
casing branches. The ring and `cold_elapsed` advance only while primary water
actually flows through the space-heating HX (`pmod > 0.0 && !ww`); during a
pump stop or DHW priority the front is held. The latch exits when
`T_hx >= 28 °C` or `elapsed >= L_cold_start + 5*tau_cold_front`. The front is
fitted on **TUNE-1 only**.

### 2.4 Staleness and causality

Every episode is loaded with a 30-minute warm-up buffer and ASOF joins. No
backward fill is used. Telemetry is declared valid when

```
tb_age_s <= 900 s  AND  bp_age_s <= 14400 s  AND  pmod_age_s <= 14400 s
```

The boiler flow temperature is continuously updated, so `tb_age_s` is the
reliable gateway-drop indicator; burner and pump modulation are event-driven
and can legitimately remain unchanged for hours. During an invalid sample the
simulator holds every state variable and the ring buffer does not replay stale
water. The same guard is compiled into the C++ header.

At the first evaluation sample the water states are **re-anchored** to the
measured manifold and boiler temperatures, while the causal pump filters and
event timers retain the 30-minute warm-up history. This prevents an
unobserved pre-window transient from biasing the target window; without it,
an idle episode that follows a DHW draw would start from a stale warm-up
temperature. FIT episodes and synthetic unit tests use no warm-up and no
re-anchor.

### 2.5 Identical initial timers

Both implementations start with `dhw = 0`, `post_dhw = 9999`, `pump_off =
9999`, `burner_off = 9999`, `cold_start = 9999`, `tb_active = tb[0]`,
`sim = t_inc[0]`. The v5 implementation started `pump_off` and `burner_off` at
zero, which silently disabled the first hold/casing decision.

---

## 3. FIT identification (no TUNE or VAL data)

### 3.1 Regime effectiveness ratios (1–24 Dec)

| Regime | Active minutes | Mean effectiveness ε | Mean ΔT_hx (°C) |
|---|---|---|---|
| A (floor only) | 7530 | 0.9398 | 2.70 |
| B1 (combined, 1 rad) | 1646 | 0.9476 | 2.37 |
| B2 (combined, 2 rads) | 180 | 0.9436 | 2.18 |
| C (radiators only) | 1446 | 0.9080 | 3.82 |

Ratios relative to Regime A: **B1/A = 1.0083**, **B2/A =
1.0040**, **C/A = 0.9661**. These ratios scale the
FIT-1 `eps_A`; they are not re-fitted on TUNE or VAL.

### 3.2 `tau_hx` and `eps_A` (FIT-1)

| Parameter | Value | FIT-1 RMSE |
|---|---|---|
| `tau_hx` | 77.6747 s | 0.3525 °C |
| `eps_A` | 0.945010 | 0.3525 °C |

### 3.3 DHW diverter (FIT-DHW)

| Parameter | Value |
|---|---|
| `dhw_delay_s` | 90.0 s |
| `tau_dhw_fast` | 87.8440 s |
| FIT-DHW entry RMSE | 0.7716 °C |

### 3.4 Radiator-loop stagnation constant (FIT)

The one-step cooling ratio of radiator-only pump-off samples on the FIT window
gives a median ratio of 0.97222 per minute
(N = 1,427), hence `tau_rad_off = 2129.9 s`.
This parameter is identified on FIT and is **not** tuned on TUNE or VAL.

### 3.5 Hot-manifold idle cooling constant (FIT)

Pure-idle FIT samples with no secondary pump and no DHW
(`~p1 & ~p2 & ~p3 & ~ww`, `T_incoming - T_room > 10 °C`) give a median
one-step cooling ratio of 0.98995 per minute
(N = 2,169), hence `tau_idle = 5939.9 s`.
This replaces the v5 `tau_ambient = 36000 s` design constant, which was derived
from cold DHW idle episodes already near room equilibrium. It is identified on
FIT only and is never tuned on TUNE or VAL.

### 3.6 Hydraulic decoupling (corrected claim)

On the FIT window, settled Regime A (N = 7,530):

| Correlation | Value |
|---|---|
| `r(mixer_state, T_incoming)` | -0.3786 |
| `r(mixer_state, ΔT_hx)` | -0.0361 |
| `r(T_boiler, T_incoming)` | +0.7931 |

The full-season Regime A value reported in v5 was `r = -0.0089`. That value
shows **no material linear association observed during settled Regime A
operation**; the FIT-only recomputation (-0.0361) is
consistent with that reading. Valve position does not materially explain the
steady HX temperature drop, while boiler supply remains strongly coupled to
incoming supply.

---

## 4. TUNE results (TUNE-1 and TUNE-2 only)

### 4.1 Dedicated cold-start fit (TUNE-1)

Grid search on the first five minutes of TUNE-1 selected
`L_cold_start = 120.0 s` and
`tau_cold_front = 5.0 s`
(cold-window RMSE = 0.2193 °C).

### 4.2 Coordinate grid search

The objective is the equal-weight mean of the TUNE-1 and TUNE-2 target-window
RMSEs. `tau_rad_off` is fixed at its FIT value. Three coordinate passes over
the required grid were run; the accepted moves are listed below.

| Pass | Parameter | Value | Objective (mean RMSE) | TUNE-1 RMSE | TUNE-2 RMSE |
|---|---|---|---|---|---|
| 0 | initial | n/a | 1.682917 | 0.7831 | 2.5827 |
| 1 | L_pump_off | 60.0 | 1.682882 | 0.7831 | 2.5827 |
| 1 | L_burn_off | 10.0 | 1.680211 | 0.7777 | 2.5827 |
| 1 | tau_burn_off | 3200.0 | 1.665064 | 0.7474 | 2.5827 |

The grid also covered `tau_cutoff` (60–300 s), `L_cold_start` (100–180 s),
`tau_cold_front` (0–80 s) and `post_dhw_delay_s` (0–180 s). No candidate for
those parameters improved the objective beyond the values already selected by
the dedicated cold-start fit, so they remained unchanged.

### 4.3 Final TUNE metrics (reported separately from VAL)

| Episode | Samples | RMSE (°C) | MAE (°C) | P95 (°C) | R² | Bias (°C) |
|---|---|---|---|---|---|---|
| TUNE-1 | 1753 | 0.747 | 0.313 | 1.128 | 0.9866 | 0.123 |
| TUNE-2 | 211 | 2.583 | 1.247 | 6.965 | 0.9797 | -0.802 |

TUNE-1 is dominated by active heating, a cold start, burner cutoffs and a final
shutdown; TUNE-2 is dominated by a DHW divert and return. TUNE-2 retains a
larger residual because the diverter travel time in that episode is longer than
the FIT-DHW-fitted 90 s. That is an honest, out-of-sample residual: the entry
delay is **not** re-fitted on TUNE-2.

### 4.4 Final v6.1 parameters

| Parameter | Value | Source |
|---|---|---|
| `tau_hx` | 77.6747 s | FIT-1 |
| `eps_A` | 0.945010 | FIT-1 |
| `eps_B1` | 0.952898 | FIT ratio × `eps_A` |
| `eps_B2` | 0.948818 | FIT ratio × `eps_A` |
| `eps_C` | 0.913015 | FIT ratio × `eps_A` |
| `dhw_delay_s` | 90.0 s | FIT-DHW |
| `tau_dhw_fast` | 87.8440 s | FIT-DHW |
| `post_dhw_delay_s` | 90.0 s | TUNE |
| `L_pump_off` | 60.0 s | TUNE |
| `L_burn_off` | 10.0 s | TUNE |
| `tau_burn_off` | 3200.0 s | TUNE |
| `tau_cutoff` | 150.0 s | TUNE |
| `L_cold_start` | 120.0 s | TUNE-1 |
| `tau_cold_front` | 5.0 s | TUNE-1 |
| `tau_rad_off` | 2129.9 s | FIT |
| `tau_idle` | 5939.9 s | FIT pure-idle cooling |

---

## 5. VAL results (untouched episodes)

| ID | Episode | Samples | RMSE (°C) | MAE (°C) | P95 (°C) | R² | Offline Centered-Filter Comparison RMSE (°C) |
|---|---|---|---|---|---|---|---|
| VAL-1 | VAL-1 Regime A Heated Run 2 | 1801 | 0.868 | 0.560 | 1.483 | 0.7463 | 0.868 |
| VAL-2 | VAL-2 Regime B Combined Run 1 | 991 | 0.760 | 0.657 | 1.364 | 0.9680 | 0.755 |
| VAL-3 | VAL-3 Regime B Combined Run 2 | 931 | 0.830 | 0.750 | 1.244 | 0.9806 | 0.830 |
| VAL-4 | VAL-4 Hydraulic Transition A->B | 901 | 0.555 | 0.485 | 1.096 | 0.9900 | 0.555 |
| VAL-5 | VAL-5 Cold Start + Mid-Run Cutoffs + Final Shutdown | 2065 | 1.916 | 1.260 | 4.414 | 0.9719 | 1.916 |
| VAL-6 | VAL-6 Circulating DHW Divert + Return | 391 | 4.422 | 2.952 | 11.204 | 0.9207 | 4.422 |
| VAL-7 | VAL-7 DHW Priority Divert (Idle) | 181 | 0.199 | 0.191 | 0.272 | 0.7063 | 0.199 |

**Offline Centered-Filter Comparison.** The last column applies a centred
31-sample median filter to the pump signals before simulation. It is an
offline diagnostic comparison, **not** a causal result and **not** an upper
bound on achievable causal performance. The causal RMSE is the sign-off metric.

**Space-heating validation (VAL-1..VAL-5).** Mean causal RMSE =
0.986 °C, mean MAE = 0.742 °C, mean P95 =
1.920 °C, mean R² = 0.9314; pooled RMSE =
1.249 °C, pooled MAE = 0.807 °C, pooled R² =
0.9758.

**DHW validation (VAL-6, VAL-7).** Reported in the table above and kept
separate from the space-heating summary. VAL-6 is the new circulating-DHW
episode; VAL-7 is the untouched idle-DHW episode. VAL-7 has very little
target variance because it is an idle episode, so its R² is dominated by the
small denominator and is not a meaningful goodness-of-fit statistic there;
RMSE and MAE are the relevant metrics.

---

## 6. Four-way shutdown comparison on TUNE-1

All four rows use the same TUNE-1 warm-up and target window. The historical
v3/v4/v5 rows use the frozen v5 constants so that the comparison isolates the
architecture; the v6 row uses the v6 fitted constants. This is stated
explicitly because the constants differ.

| Model | Overall RMSE (°C) | Cold-start RMSE (°C) | Mid-run cutoffs RMSE (°C) | Final shutdown RMSE (°C) | Max |error| (°C) |
|---|---|---|---|---|---|
| v3 Baseline | 2.428 | 3.664 | 5.027 | 13.344 | 21.64 |
| v4 Clamped | 1.200 | 3.664 | 3.220 | 4.318 | 8.12 |
| v5 Two-Stage | 0.920 | 3.664 | 2.617 | 1.748 | 7.44 |
| v6 Final | 0.747 | 0.221 | 2.458 | 1.771 | 4.85 |

**Residuals, quantified.** The cold-start window is the first five minutes of
the target (or until measured `T_incoming` reaches 28 °C). The mid-run cutoffs
are the 2 detected burner-off / pump-on windows (indices 1098-1150, 1820-1896).
The final shutdown is the last pump-off run of at least three minutes
(indices 1896-1933 if detected).

Relative to v5, v6 reduced the overall RMSE by 18.8%,
reduced the cold-start RMSE by 94.0%, reduced the mid-run-cutoff RMSE by 6.1%, and
increased the final-shutdown RMSE by 1.3%. The final-shutdown residual is
`1.771 °C` (v5: `1.748 °C`), and
the mid-run-cutoff residual is `2.458 °C` (v5:
`2.617 °C`). The overall, cold-start and mid-run
residuals improve; the final-shutdown residual is marginally worse. These
residuals are **reduced, not eliminated**; the v5 wording "completely
eliminated" was overstated and is withdrawn.

---

## 7. Python/C++ parity and synthetic unit tests

The Python script writes `step2_hx_dynamics_v6.hpp` with the exact fitted
constants, writes `step2_hx_parity_harness.cpp`, compiles with
`g++ -O2 -std=c++17`, and compares every simulated row.

| Parity item | Result |
|---|---|
| Compiler | `/usr/bin/g++` |
| Flags | `-O2 -std=c++17` |
| Episodes compared | FIT-1, FIT-DHW, TUNE-1, TUNE-2, VAL-1..VAL-7, UT1..UT5 |
| Rows compared | 15,769 |
| Max absolute difference | `4.999e-11 °C` |
| Tolerance | `< 1e-4 °C` |
| Cold-start ring length | 12 samples |

| Synthetic test | Result | Expected | Detail |
|---|---|---|---|
| UT1_LowLift | PASS | ~25.03 C | final=25.0300 C |
| UT2_PumpOffHoldPlunge | PASS | hold then <30 C | hold_delta=0.0000 C, final=20.00 C |
| UT3_BurnerOffPumpOn | PASS | 25-50 C (casing decay) | final=36.76 C |
| UT4_DHWEntryReturn | PASS | DHW <32 C, recovery >55 C | dhw_end=25.00 C, rec_end=67.53 C |
| UT5_ColdStartWarmup | PASS | flat <21 C then >45 C | flat=20.00 C, warm=53.08 C |

The five tests cover the low-lift steady state (→ 25.03 °C), pump-off hold and
plunge, burner-off/pump-on casing decay, DHW entry and return, and cold-start
warm-up. All five are executed against the shipped v6.1 constants, and the same
five synthetic frames are included in the C++ parity input.

---

## 8. Corrections to the v5 report

1. **Four-way shutdown table.** v5 compared only v3 and v5. v6 adds v4
   (clamped) and v6 (final) on TUNE-1, with cold-start, mid-run-cutoff and
   final-shutdown residuals separated.
2. **"Completely eliminated."** Withdrawn. v6 reports the residual RMSE for
   each failure mode instead of claiming elimination.
3. **"Centered RMSE."** Renamed to **Offline Centered-Filter Comparison** and
   explicitly described as a diagnostic, not an upper bound.
4. **`r = -0.0089`.** The correct interpretation is "no material linear
   association observed during settled Regime A operation", not "the valve has
   no effect anywhere". The FIT-only recomputation is reported alongside it.
5. **Leakage.** The old VAL-5 and VAL-6 are now TUNE-1 and TUNE-2; two brand-new
   untouched episodes replace them as VAL-5 and VAL-6.

---

## 9. Limitations and residual risks

- **DHW entry timing and depth.** The FIT-DHW diverter delay is 90 s. TUNE-2
  and VAL-6 exhibit a longer effective entry delay and a shallower measured
  plunge than the model's `T_return` target; the model therefore leads and
  over-deepens the plunge, leaving a residual. Re-fitting the delay or the
  DHW target on TUNE-2 would be leakage and is deliberately not done.
- **Idle cooling after all pumps stop.** VAL-5 contains long periods with no
  pump running. The idle branch relaxes toward `T_room` with the FIT-identified
  `tau_idle = 5939.9 s`; this removes the v5
  `tau_ambient = 36000 s` design constant that badly under-cooled a hot
  manifold. A single global constant still cannot represent every
  manifold/air-path configuration, so a residual remains, and the branch is
  not a full building thermal model.
- **Mid-run pump-off with radiator circulation.** VAL-5 also contains two long
  primary-pump-off runs with only radiator pumps running. The FIT-identified
  `tau_rad_off = 2129.9 s` is a single global
  constant; the two runs cool at somewhat different effective rates. This is
  the second-largest contributor to the VAL-5 residual.
- **Cold-start fit.** `L_cold_start` and `tau_cold_front` are fitted on one
  TUNE-1 cold start and validated on the independent VAL-5 cold start. A
  multi-episode cold-start fit would be preferable once more cold starts are
  labelled.
- **Sensor noise and event-driven logs.** The staleness guard uses
  `tb_age_s <= 900 s`; a shorter dropout that leaves the last boiler
  temperature plausible is not detectable from the data alone.
- **Constants differ across the four-way table.** v3/v4/v5 use frozen v5
  constants; v6 uses the v6 fitted constants. The table isolates architecture,
  not a single-constant sweep.

---

## 10. Reproduction

```bash
# Preferred: the project uv environment
uv run python simulation/step2_hx_dynamics_v6.py

# Optional overrides
HEATPUMPS_DB_PATH=/path/to/heatpumps.duckdb \
HEATPUMPS_OUT_DIR=/path/to/out \
uv run python simulation/step2_hx_dynamics_v6.py

# The C++ parity harness is generated and compiled automatically:
g++ -O2 -std=c++17 -o simulation/step2_hx_parity_harness \
    simulation/step2_hx_parity_harness.cpp
./simulation/step2_hx_parity_harness simulation/step2_hx_parity_input.csv
```

No hard-coded `/home/gume/...` path is required. `UV_LIB_PATHS` is only an
optional `os.path.exists()` fallback for an un-activated uv cache.

---

## 11. Artifacts

| Artifact | Purpose |
|---|---|
| `simulation/step2_params_v6.json` | Versioned parameter and metric export |
| `simulation/step2_params.json` | Canonical alias of the v6.1 export |
| `simulation/step2_hx_dynamics_v6.hpp` | Auto-generated C++ implementation with exact constants |
| `simulation/step2_hx_parity_harness.cpp` | C++ parity harness |
| `simulation/step2_hx_parity_input.csv` | Row-level parity input |
| `simulation/step2_hx_parity_harness` | Compiled harness (g++ -O2) |
| `simulation/step2_hx_dynamics_report_v6.md` | This report |
| `simulation/tune1_diagnostic_overlay_v6.png` | TUNE-1 diagnostic overlay |
