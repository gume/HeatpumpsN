# Analysis Report — Heat Pump / Mixer System (DuckDB-derived parameters)

**Database:** `/home/gume/HeatpumpsN/analysis/heatpumps.duckdb` (built from 15 CSV.gz series; 20,421,477 rows; all row counts verified).  
**Analysis window used for heating-specific work:** 2025-12-01 → 2026-05-02 (active heating), plus full-year context.  
**Timezone:** all timestamps treated as UTC.

---

## 1. Dataset / heating windows

Active heating (rising `mixer_forward_temperature`, range > 4 K/day) occurs in:

| Window | Days |
|---|---|
| 2025-12-01 → 2025-12-17 | 17 |
| 2025-12-22 → 2026-04-14 | 114 |
| 2026-04-18 → 2026-04-22 | 5 |
| 2026-04-26 → 2026-05-02 | 7 |
| 2026-09-15 | 1 (isolated, likely test) |

Recent data (May–Oct 2026) is non-heating; pump runs there are unfulfilled demand (warning, not error). Radiator pumps (1/2) cycle rapidly (median run 1 min) and do not start with floor heating; pump 3 (floor) runs 2–18 min (median 2 min, p90 18 min, max 183 min).

---

## 2. Mixer position tracking (`mixer_state` = DR hypothesis) — VERIFIED (a)

- Changes occur in discrete steps of **≈ 0.69 % per pulse** (top deltas 0.68–0.70, both directions).
- Movement rate ≈ 0.69 %/s → full 0→85 % travel ≈ **123 s** → `travel_time_ms ≈ 120 000 ms`.
- Idle periods produce no rows (event-driven reporting); median inter-sample gap 1.0 s during movement, p99 217 s.
- Max observed `mixer_state` = 88.98 (documented sw cap 85; slight overshoot observed).
- Daily reset behaviour: after long gaps (>1 h) value restarts near 0–0.7, consistent with a daily re-home to 0.

Conclusion: `mixer_state` is the dead-reckoning estimate. Direct DR-vs-encoder comparison is valid.

---

## 3. Encoder characterization (b/a)

- Range 0–100, 525 094 samples, median spacing 0.51 s.
- Noise floor / quantization: median absolute sample-to-sample delta = **0.4 units** (p90 also 0.4); occasional jumps up to 92 (real movement or glitches).
- Staleness: **3 829 gaps > 300 s**, 758 gaps > 3 600 s, max gap 9.05 days.
- Gap distribution: median 0.51 s, p95 29.99 s, p99 229.1 s.
- → `encoder_staleness_s = 300 s` is supported (covers p99 of gaps).

---

## 4. DR vs. encoder divergence (b)

Aligned pairs (≤5 s): 287 815.

| Statistic | Value |
|---|---|
| Median \|DR − enc\| | 2.06 |
| p90 | 5.11 |
| p95 | 18.85 |
| p99 | 44.49 |
| Mean (DR − enc) | +2.58 (DR reads high) |
| Correlation | 0.932 |

Divergence grows with position (at DR ≈ 70, encoder ≈ 65; at DR ≈ 80, encoder ≈ 74.5). Normal operation therefore shows 5–6 units of divergence at high positions.

Recommendation: `recalc_threshold` baseline **10–15 %** (covers normal high-position divergence + noise; current observed worst-case normal ≈ 6). Values below 10 risk false `WARN_ENCODER_DIVERGED`.

---

## 4b. Encoder vs DR scale factor & travel time (a/b)

Linear fit `encoder ~ a·DR + b` over 170 021 aligned pairs (heating season, ≤5 s):

- **a = 0.848**, b = 5.40, R² = 0.737, residual σ = 10.5 units.
- Segment slopes: DR 0–30 → 0.73; 30–60 → 0.85; 60–90 → 0.74. Median ratio (enc/DR) for DR 60–90 is 0.973.
- The relationship is noisy and nonlinear, not a clean scale factor; the old "0.93 ratio" was a mid-range approximation.

**Travel time.** Old hardware (ESPHome `open_duration`) = **145 s for 0–100 %** → 123 s for 0–85 % (sw cap). Data-driven movement rate = 0.69 %/s → 123 s for 0–85 %, matching hardware.

| Quantity | Value |
|---|---|
| Hardware `travel_time_ms` (authoritative) | 145 000 ms (0–100 %) |
| Data-driven (0–85 %) | 123 000 ms |
| Encoder-calibrated (if forcing DR→encoder, slope 0.848) | 171 000 ms (0–100 %) |
| Encoder-calibrated (mid-range ratio 0.93) | 156 000 ms (0–100 %) |

Recommendation: keep **`travel_time_ms = 145 000 ms`** (hardware-measured). The encoder/DR discrepancy is a calibration/slip issue, not travel time; do not retune travel time to the encoder. The 10–15 % `recalc_threshold` already covers this drift.

## 5. Mixing thermodynamics (b, low confidence — superseded in part by §5d)

*Note: the corrected mixing model (§5d) uses `T_incoming` as the hot input and `T_return_floor` as the cold input and reaches R² 0.764. The instantaneous regressions below are retained for history.*

Regression of `T_forward` on candidate blending models over 2 414–12 271 aligned heating samples. Best R² ≈ 0.43 (`incoming + return`), indicating a strong dynamic/lag component not captured by instantaneous mixing.

| Model (T_fwd ≈ …) | ff_hot | ff_cold | rad coupling | intercept | R² |
|---|---|---|---|---|---|
| pos·ff_boiler·T_boiler + (1−pos)·ff_incoming·T_incoming | 0.33 | 0.17 | −1.98 K | 15.5 | 0.36 |
| pos·ff_boiler·T_boiler + (1−pos)·ff_return·T_return_floor | 0.46 | 0.81 | −1.76 K | 6.9 | 0.39 |
| pos·ff_incoming·T_incoming + (1−pos)·ff_return·T_return_floor | 0.42 | 0.61 | −2.99 K | 12.4 | 0.43 |

Baseline recommendation (per handover, use T_incoming as return-side input):  
`ff_boiler ≈ 0.33`, `ff_incoming ≈ 0.17`, `rad_coupling_ff ≈ −2.0 K`.  
Confidence **low** — re-estimate with a dynamic (lagged) model before adopting; instantaneous fit is poor.

Time constant / `delay_s`: not quantified (requires step-response analysis); baseline **60–120 s** from observed movement/thermal inertia.

---

## 5b. Lagged mixing model (dead time & time constant) (b)

Cross-correlation of 1-min-bucketed series (`mixer_state` vs `mixer_forward_temperature`) during heating:

- Change-to-change (derivative) correlation peaks at a lag of **2 min** → estimated **dead time / transport delay ≈ 120 s**.
- Level correlation peaks at **2–3 min** (r ≈ 0.83).
- Regression R² improves gradually up to **8–9 min** (0.707 vs 0.697 at 0 lag), indicating a slow thermal time constant on top of the dead time.

Feedforward regression on lagged (3-min) 1-min-bucketed data, handover model  
`T_fwd ≈ pos·ff_boiler·T_boiler + (1−pos)·ff_incoming·T_incoming − rad_coupling_ff·rad_active`:

| Coefficient | Value | (instantaneous 10-s regression was 0.33 / 0.17 / −2.0) |
|---|---|---|
| `ff_boiler` | **0.32** | — |
| `ff_incoming` | **0.41** | — |
| `rad_coupling_ff` | **−0.83 K** | — |
| intercept | 9.4 K | (unmodeled baseline / heat loss) |
| R² | 0.705 | (vs 0.36 instantaneous) |
| RMSE | 3.75 K | — |

Using `T_return_floor` as the cold side instead of `T_incoming` gives R² 0.631 (worse), confirming the handover guidance to use `T_incoming`.

First-order (time-constant) estimate: residual autocorrelation after the lagged feedforward model gives lag-1 r = 0.659 → time constant **τ ≈ 2.4 min** (not 8–10 min; the earlier 8–9 min R² plateau was smoothing, not dynamics). A dynamic model that also uses `T_fwd(t−1)` reaches R² 0.87 (RMSE 2.50 K) but shrinks the ff coefficients (0.12/0.18) because the lagged temperature absorbs the dynamics — use it only as a closed-loop predictor, not for ff tuning.

Recommendation: `delay_s ≈ 120 s` (dead time), thermal time constant **τ ≈ 2.4 min**; `ff_boiler ≈ 0.32`, `ff_incoming ≈ 0.41`, `rad_coupling_ff ≈ −0.8 K`. Confidence **medium** (R² 0.71 feedforward; 0.87 with closed-loop lag term). Intercept 9.4 K remains (unmodeled heat loss / ambient); adding outside temperature barely changes R² (0.708), so outdoor temp is not a strong predictor.

## 5c. Nonlinear valve characteristic & pump coupling (b)

**Method.** Regressions were repeated on 1-min-bucketed heating data with pos lagged by the 2-min dead time. Segmentation by `pos` bin, by radiator-pump state, and by continuous pump power. Valve characteristic estimated as effective hot fraction  
`h = (T_fwd − T_return_floor) / (T_boiler − T_return_floor)`.

**Findings.**

1. **Valve characteristic is nonlinear.** Segmented fits show `ff_boiler` falling with position (0.35 at pos 0–20 → 0.10 at pos 80–100) while `ff_incoming` rises (0.28 → 1.30). The simple flow-fraction `h` is nearly flat (~0.05–0.15) across the whole position range, so position is not a linear proxy for hot-water fraction; the mixer behaves as an equal-percentage / highly nonlinear valve. Constant ff (0.32/0.41) remains the recommended baseline; a position-dependent gain should be handled by the closed-loop PI, not the feedforward.

   | pos bin | n | ff_boiler | ff_incoming | rad coupling | intercept | R² |
   |---|---|---|---|---|---|---|
   | 0–20 | 15 300 | 0.35 | 0.28 | −1.91 | 13.79 | 0.64 |
   | 20–40 | 7 367 | 0.29 | 0.36 | −1.03 | 11.00 | 0.71 |
   | 40–60 | 54 821 | 0.24 | 0.51 | −0.50 | 9.86 | 0.76 |
   | 60–80 | 43 548 | 0.22 | 0.66 | −0.86 | 9.67 | 0.73 |
   | 80–100 | 7 332 | 0.10 | 1.30 | −3.20 | 11.36 | 0.64 |

2. **Radiator-pump coupling.** When a radiator pump is running (`rad_active = 1`, n = 995) the fit degrades sharply (R² 0.26 vs 0.71) and ff coefficients drop (0.23/0.24, intercept 15.0). Using continuous pump power instead of a binary flag improves R² from 0.703 to 0.718; the rad coupling scales with pump power at ≈ −23 K per 100 W of combined radiator-pump power (≈ −0.8 to −1.0 K at typical 30–40 W). The binary `rad_coupling_ff ≈ −0.8 K` is a reasonable average; for better accuracy use the continuous pump power.

3. **Floor-pump power.** Adding floor-pump (pump 3) power as a regressor raises R² to 0.768, but its coefficient (≈ 73 K per 100 W) is a proxy for active heating/flow rather than a physical gain — keep it out of the feedforward, but it is useful as an auxiliary indicator of active floor heating.

4. **Conclusion.** The feedforward should retain constant `ff_boiler ≈ 0.32`, `ff_incoming ≈ 0.41`, `rad_coupling_ff ≈ −0.8 K` (optionally continuous pump power for rad coupling). Nonlinear valve behaviour and pump interactions are best left to the closed-loop PI / Smith predictor rather than embedded in ff.

## 5d. Physical-role verification & corrected mixing model (a/b)

**Temperature distributions during active floor heating** (pump 3 > 10 W, n = 17 404):

| Series | min | median | max | mean |
|---|---|---|---|---|
| `T_boiler` | 22.1 | 67.7 | 81.5 | 65.4 |
| `T_incoming` | 18.7 | 64.9 | 71.1 | 59.9 |
| `T_forward` | 17.8 | 34.8 | 40.8 | 33.5 |
| `T_return_floor` | 16.8 | 25.6 | 27.4 | 25.2 |

`T_incoming` (median 64.9 °C) tracks `T_boiler` (67.7 °C) and is far warmer than `T_forward` (34.8 °C) and `T_return_floor` (25.6 °C). Therefore **`T_incoming` is the HOT input** (heat-exchanger secondary / boiler-side), and `T_return_floor` is the COLD input. The handover's original formula (`pos·T_boiler + (1−pos)·T_incoming`) mis-assigned the cold side; `T_boiler` is even hotter and separated by the heat exchanger, so it should not be used directly.

**Corrected mixing model** (2-min-lagged, 1-min-bucketed, heating season):

| Model | Hot input | Cold input | R² | RMSE | ff_hot | ff_cold | rad | intercept |
|---|---|---|---|---|---|---|---|---|
| A (original) | `T_boiler` | `T_incoming` | 0.704 | 3.76 | 0.32 | 0.41 | −0.83 | 9.39 |
| **B (corrected)** | **`T_incoming`** | **`T_return_floor`** | **0.764** | **3.36** | **0.47** | **0.87** | −1.54 | 6.75 |
| C | `T_boiler` | `T_return_floor` | 0.632 | 4.19 | 0.55 | 1.35 | 0.04 | −3.56 |

Model B is the best fit and physically consistent: `T_incoming` is the hot side, `T_return_floor` the cold side. `ff_hot ≈ 0.47`, `ff_cold ≈ 0.87`, `rad_coupling ≈ −1.5 K`.

**Per-`T_boiler` band check** (Model A form, no polynomial, 2-min lag):

| T_boiler band | n | ff_boiler | ff_incoming | rad | intercept | R² | RMSE |
|---|---|---|---|---|---|---|---|
| < 40 °C | 20 621 | 0.288 | 0.439 | +1.109 | 9.65 | 0.335 | 2.31 |
| 40–50 °C | 23 808 | 0.289 | 0.448 | +0.923 | 9.53 | 0.339 | 3.30 |
| > 50 °C | 83 938 | 0.337 | 0.417 | −1.566 | 8.42 | 0.514 | 4.13 |

Feedforward gains are stable across boiler bands (ff_boiler 0.29–0.34, ff_incoming 0.42–0.45); the rad-coupling sign flips with boiler temperature, so a single average (−0.8 to −1.5 K) is a compromise. Constant feedforward + PI is retained.

## 6. DHW characterization (a/b)

Detected via `boiler_wwcharging`: **894 ON periods**.

| Statistic | Value |
|---|---|
| Median duration | 17.9 min |
| p10 / p90 | 14.4 / 25.9 min |
| Min / max | 4.9 / 252.8 min (max = stuck/missed OFF) |

Spec assumes 5–15 min; observed is **14–26 min**. DHW handling (integrator freeze, `DIV_DHW_PRIORITY_ACTIVE`) must tolerate ≥ 30 min. `boiler_heatingactive` stays ON during DHW (confirmed).

---

## 7. Circuit dynamics (a/b)

ΔT = `T_forward − T_return_floor`:

- Overall: median 8.2 K, p10 0.44 K, p5 0.25 K.
- During floor-pump-active periods (power > 10 W): median 8.75 K, p10 0.94 K, p5 0.38 K.

`min_useful_delta_t` baseline: **2 K** (≈ 16 % of floor-active samples fall below; balances collapse detection against noise). Observed floor setpoint (median `T_forward` during pump-3-active) = **34.6 °C** (IQR 32.4–35.2) → `floor_setpoint_shadow` / `setpoint_default ≈ 35 °C`.

Temperature rate: p99 |dT/dt| = 0.127 K/s, observed max 0.294 K/s → `grad_limit` baseline **0.3 K/s**. Max observed `T_forward` = 42.6 °C → `t_max_warn = 45 °C`, `t_max_hard = 50 °C` (safety margin over observed max).

---

## 8. Pump behaviour (b)

- Pump 3 (floor) runs: median 2 min, p10 1 min, p90 18 min, max 183 min.
- Pumps 1/2 (radiators): median 1 min, very short cycling (pump 2 median 1 min, p90 1 min).
- → `pump_stop_after_heating_s` baseline **300 s** (5 min) (after-heating circulation observed to be short; 300 s covers typical residual heat removal). `floor_pause_duration_s` baseline **600–900 s** (10–15 min), matching observed pump-3 run lengths and DHW scale.

---

## 9. Parameter classification table

| Parameter | Cat | Class | Baseline / value | Confidence | Source |
|---|---|---|---|---|---|
| `travel_time_ms` | 1 | (a) | ~120 000 ms (0.69 %/s) | High | DR movement rate |
| `encoder_staleness_s` | 1 | (a) | 300 s (p99 gap 229 s) | High | Encoder gaps |
| `recalc_threshold` | 1 | (b) | 10–15 % (normal div. 5–6 at high pos) | Medium | DR vs encoder |
| `setpoint_default` / `floor_setpoint_shadow` | 1/2 | (a) | 35 °C | High | Median T_fwd (pump 3 on) |
| `t_max_hard` | 1 | (b) | 50 °C (max obs 42.6) | Medium | Max T_fwd + margin |
| `t_max_warn` | 1 | (b) | 45 °C | Medium | Max T_fwd + margin |
| `grad_limit` | 1 | (a) | 0.3 K/s (max obs 0.294) | High | T_fwd rate |
| `min_useful_delta_t` | 3 | (a) | 2 K (p10 ≈ 0.9 when pump 3 on) | Medium-High | ΔT distribution |
| `ff_boiler` | 3 | (b) | 0.55 (T_boiler-based, R² 0.63) / 0.47 (T_incoming-based, R² 0.76) | Medium-Low | §5d corrected model |
| `ff_return` (`ff_incoming`) | 3 | (b) | 1.35 (T_return_floor-based, R² 0.63) / 0.87 (T_incoming-based, R² 0.76) | Medium-Low | §5d corrected model |
| `rad_coupling_ff` | 3 | (b) | −0.8 K | Medium | Lagged regression |
| `pump_stop_after_heating_s` | 1 | (b) | 300 s | Medium | Pump run durations |
| `floor_pause_duration_s` | 3 | (b) | 600–900 s | Medium | Pump 3 run lengths |
| `delay_s` | 3 | (b) | 120 s dead time (τ ≈ 2.4 min) | Medium | Cross-correlation peak 2 min |
| `encoder_check_interval_s` | 1 | (c) | 3 600 s | — | Design |
| `encoder_check_pulses` | 1 | (c) | 100 | — | Design |
| `deadband_temp` | 1 | (c) | 2 K | — | Design |
| `deadband_pos` | 1 | (c) | 0.02 | — | Design |
| `min_pulse_ms` / `max_pulse_ms` / `pulse_cooldown_ms` | 1 | (c) | hardware-dependent (not in data) | — | Design |
| `boiler_min_off_ms` | 1 | (c) | anti-short-cycle backstop | — | Design |
| `demand_timeout_s` | 1 | (c) | refresh_time + 1 min (HA) | — | Design |
| `long_pause_s` | 1 | (c) | 600 s | — | Design |
| `anti_seize_interval_s` | 1 | (c) | 30 days | — | Design |
| `test_mode_max_s` | 1 | (c) | 7 200 s | — | Design |
| `sensor_error_threshold` | 1 | (c) | 3 | — | Design |
| `t_overtemp_hysteresis` | 1 | (c) | 2.0 K | — | Design |
| `mix_safe_pos` | 1 | (c) | 0.0 | — | Design |
| `start_flush_ms`, `flush_max_pos`, `flush_delta_t_ok` | 3 | (c) | out of scope (no flush hardware) | — | Design |
| `gain`, `kp`, `ki` | 3 | (c) | tuning (observer) | — | Design |
| `summer_mode` | 2 | (c) | operational flag | — | Design |

---

## 10. Key recommendations / follow-ups

1. **Heating analysis must use the Dec 2025–Apr 2026 windows**; May–Oct data is non-heating (pump runs without temperature rise = warning).
2. **DR hypothesis confirmed** — direct DR-vs-encoder comparison and divergence statistics are valid.
3. **DHW lasts 14–26 min (median 18)**, not 5–15 min; freeze/suppression logic must tolerate ≥ 30 min.
4. **Encoder is frequently stale** (> 5 min gaps: 3 829 occurrences); `encoder_staleness_s = 300 s` is appropriate and will legitimately fire often.
5. **`recalc_threshold` should be ~10–15 %**, not the 5 % implied by median divergence, because normal high-position operation shows 5–6 % systematic divergence.
6. **Mixing model improved with lag** — 1-min-bucketed lagged regression reaches R² ≈ 0.71 (0.764 with corrected hot/cold roles) with dead time ≈ 2 min and time constant τ ≈ 2.4 min; ff values upgraded from provisional to medium confidence.
7. **Radiator pumps cycle in ~1-min bursts**; coordination logic must handle frequent rad on/off without triggering floor ΔT-collapse pauses.
8. **Valve is nonlinear (equal-percentage-like)**: ff_boiler falls 0.35→0.10 across the position range while ff_incoming rises 0.28→1.30; keep ff constant and let the PI/Smith predictor handle nonlinearity. Rad coupling should optionally use continuous pump power (−23 K/100 W) instead of a binary flag.
9. **Max floor temperature observed 42.6 °C**; safety bounds (45/50 °C) have only ~2–7 K margin — verify against boiler setpoint before finalizing.

---

*Generated from `/home/gume/HeatpumpsN/analysis/heatpumps.duckdb` via DuckDB 1.5.2 (Python).*
