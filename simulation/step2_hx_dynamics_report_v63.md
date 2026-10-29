# Phase 2 - Version 6.3: Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)

**Project:** Hardware-in-the-Loop (HIL) Floor Heating Simulator
**Version:** 6.3
**Generated:** 2026-10-29 02:00:21
**Script:** `simulation/step2_hx_dynamics_v63.py`
**Database:** `analysis/heatpumps.duckdb` (read-only)
**Generated C++:** `simulation/step2_hx_dynamics_v63.hpp`

## 0. Sign-off summary

**Holdout result: FAIL.** The Phase 0 holdout (VAL-8, VAL-9)
was selected from telemetry only, frozen and committed before any model change,
and evaluated once after the parameter freeze. Its result is reported as is.

| Provenance item | Value |
|---|---|
| Phase 0 commit | `facafae` |
| Report commit | `facafaec6df652dfb72c6a70cbb6674487b21933` |
| `gates_v63.json` SHA-256 | `270ee734cb4b458ac8f0891e66048098c4ad8e9cb42745182360434a801efbbf` |
| `holdout_v63.json` SHA-256 | `2fb8386ed273ea6803a097a38bc03b9f810f62f347e0d0cf58c1cb613d88a4b2` |
| Final parameter dict SHA-256 | `bf9fe353e10a2de59fb58e5461e684967105cff14827a7d0c07b6ad0cae2b31d` |

VAL-5 (2026-02-26) is the **observed holdout (seen in v6.2)**. It is evaluated
and reported in Section 5, but it cannot support a sign-off. The untouched
subset is exactly the Phase 0 holdout.

### 0.0 Frozen gates

| Suite | Metric | Threshold | Actual | Result | Margin | Margin % | Note |
|---|---|---|---|---|---|---|---|
| space_heating | Mean episode RMSE (degC) | < 1.25 | 3.6279 | FAIL | -2.3779 | -190.23% |  |
| space_heating | Pooled RMSE (degC) | < 1.5 | 3.6283 | FAIL | -2.1283 | -141.89% |  |
| space_heating | Mean episode MAE (degC) | < 0.85 | 2.4377 | FAIL | -1.5877 | -186.79% |  |
| space_heating | Minimum per-episode R2 | > 0.9 | 0.9597 | PASS | 0.0597 | +6.64% |  |
| dhw | DHW plunge RMSE (degC) | < 4.5 | 15.8523 | FAIL | -11.3523 | -252.27% |  |
| dhw | DHW per-episode R2 | > 0.9 | -1.7756 | FAIL | -2.6756 | -297.29% |  |

A margin below 1% of the threshold is
labelled marginal; none of the gates is marginal.

### 0.1 Attribution of the observed v6.2 VAL-5 failure

The Phase 1 per-branch and per-window breakdown of the v6.2 parameters is
recomputed in this run:

| Branch | Samples | RMSE (degC) | Bias (degC) |
|---|---|---|---|
| active_heating | 1431 | 1.808 | 0.958 |
| circ_unheated_cutoff | 989 | 3.800 | -1.949 |
| dhw_divert | 226 | 2.002 | 0.541 |
| idle | 1183 | 4.967 | -4.563 |
| casing_decay | 122 | 1.023 | -0.404 |
| post_dhw_return | 16 | 0.442 | -0.439 |
| stale_hold | 346 | 3.316 | -3.304 |
| pump_off_hold | 8 | 1.159 | 0.854 |

| Window | Samples | RMSE (degC) | Bias (degC) |
|---|---|---|---|
| cold_start | 30 | 4.264 | -3.888 |
| cutoff_0 | 21 | 0.737 | -0.138 |
| cutoff_1 | 22 | 0.507 | 0.185 |
| cutoff_2 | 23 | 0.913 | -0.820 |
| cutoff_3 | 18 | 1.875 | -1.724 |
| run_down_0 | 23 | 0.642 | -0.377 |
| final_shutdown | 496 | 1.297 | -0.228 |

The largest contributions are the idle branch
(4.967 degC) and the
cold_start window (4.264 degC).
Re-running the same v6.2 parameters with the exact v6.2 staleness rule holds
346 VAL-5 samples; the v6.3 rule holds
0. Phase 1 showed the v6.2 holds were caused by
event-driven burner/pump ages while the analog signals were fresh, which is why
v6.3 moves those signals to the event-style rule.

## 1. Split and disjointness

All FIT, TUNE, observed VAL and Phase 0 holdout episodes are strictly disjoint,
including the 30-minute warm-up buffers. The assertion is executed in
`main()` and uses the same episode registry as the evaluation. FIT-DHW is a
dedicated sub-window of the FIT period; its rows are excluded from the FIT
ratio identification so that the two FIT windows do not overlap in any
estimator.

| Partition | Episode | Window | Role |
|---|---|---|---|
| FIT | FIT-1 | 2026-01-14 00:00:00+01:00 -> 2026-01-14 04:00:00+01:00 | tau_hx / eps_A identification (ends 30 min before VAL-4 warm-up) |
| FIT | FIT-DHW | 2025-12-03 08:18:00+01:00 -> 2025-12-03 08:45:00+01:00 | dhw_delay_s / tau_dhw_fast identification |
| TUNE | TUNE-1 | 2025-12-27 16:06:00+01:00 -> 2025-12-27 20:58:00+01:00 | Cutoff / cold-start / post-DHW tuning |
| TUNE | TUNE-2 | 2026-01-01 14:50:00+01:00 -> 2026-01-01 15:25:00+01:00 | DHW return / post-DHW tuning (in-partition) |
| TUNE | TUNE-3 | 2026-02-08 04:27:00+01:00 -> 2026-02-08 10:11:00+01:00 | Model-selection episode (former VAL-5); idle/cutoff structure selection |
| VAL | VAL-1 | 2026-01-15 00:00:00+01:00 -> 2026-01-15 05:00:00+01:00 | Untouched validation |
| VAL | VAL-2 | 2026-02-19 14:40:00+01:00 -> 2026-02-19 17:25:00+01:00 | Untouched validation |
| VAL | VAL-3 | 2026-01-13 05:00:00+01:00 -> 2026-01-13 07:35:00+01:00 | Untouched validation |
| VAL | VAL-4 | 2026-01-14 05:00:00+01:00 -> 2026-01-14 07:30:00+01:00 | Untouched validation |
| VAL | VAL-5 | 2026-02-26 00:00:00+01:00 -> 2026-02-26 12:00:00+01:00 | Observed holdout (seen in v6.2) - reported, cannot support sign-off |
| VAL | VAL-6 | 2026-02-18 05:50:00+01:00 -> 2026-02-18 06:55:00+01:00 | New untouched validation (replaces old VAL-6) |
| VAL | VAL-7 | 2026-01-03 03:20:00+01:00 -> 2026-01-03 03:50:00+01:00 | Untouched validation |
| HOLDOUT | VAL-8 | 2026-03-16T00:00:00+01:00 -> 2026-03-16T12:00:00+01:00 | untouched v6.3 holdout, selected from telemetry only |
| HOLDOUT | VAL-9 | 2026-03-22T12:00:00+01:00 -> 2026-03-23T00:00:00+01:00 | untouched v6.3 holdout, selected from telemetry only |

Observed episodes (VAL-1..VAL-7) were seen in v5/v6.1/v6.2 and are labelled
"previously observed". Only the Phase 0 holdout is untouched.

## 2. Model

The HX outlet is a first-order lag toward the secondary-return-referenced
target:

```
T_hx[k] = (1 - a) * T_hx[k-1] + a * (T_return[k] + eps * (T_boiler,eff[k] - T_return[k]))
a = DT / (tau_hx + DT)
```

The v6.3 branch structure is unchanged from v6. The changes are in
identification, staleness, window detection and reporting. Pump states are
debounced with the same strictly causal trailing majority used by the C++
header.

### 2.1 Staleness

Telemetry is valid when `tb_age_s <= 900` and
`tret_age_s <= 900`. Burner, pump modulation and all pump-power
ages are event-driven: they invalidate a sample only when their own age exceeds
`14400` s **and** `tb_age_s` is also stale. Target metrics exclude
stale measured `T_incoming` samples. Stale-hold counts per episode in this run:

| Episode | stale_hold samples |
|---|---|
| VAL-1 | 0 |
| VAL-2 | 0 |
| VAL-3 | 10 |
| VAL-4 | 0 |
| VAL-5 | 0 |
| VAL-6 | 0 |
| VAL-7 | 0 |
| VAL-8 | 0 |
| VAL-9 | 1 |
| TUNE-1 | 0 |
| TUNE-2 | 0 |
| TUNE-3 | 1 |

The VAL-5 failure mechanism (event age without analog staleness) is therefore
removed, while genuine gateway dropouts still hold the simulator state.

## 3. FIT identification (FIT data only)

### 3.1 Effectiveness (input-only mask)

The v6.3 mask uses inputs only: `bp > 5`, `tb - tret > 10`, all pump states
stable for at least five minutes, no DHW, and fresh telemetry. Old and new
estimates:

| Regime | New N | New eps | Old N | Old eps |
|---|---|---|---|---|
| A | 5858 | 0.9386 | 7524 | 0.9398 |
| B1 | 553 | 0.9169 | 1645 | 0.9476 |
| B2 | 60 | 0.8999 | 180 | 0.9436 |
| C | 463 | 0.9029 | 1097 | 0.8993 |

Ratios relative to Regime A: new B1/A = 0.9769,
B2/A = 0.9588, C/A = 0.9620;
old B1/A = 1.0083, B2/A = 1.0040,
C/A = 0.9569. The FIT-1 `tau_hx`/`eps_A` loss uses the same
input-based mask.

### 3.2 `tau_hx` and `eps_A`

| Parameter | Value | FIT-1 RMSE (degC) | Fresh samples | Excluded stale |
|---|---|---|---|---|
| `tau_hx` | 79.8211 s | 0.3240 | 1,110 | 20 |
| `eps_A` | 0.944963 | 0.3240 | 1,110 | 20 |

FIT-1 now ends before VAL-4's warm-up begins, so the identification window no
longer overlaps the validation episode.

### 3.3 DHW diverter (FIT-DHW)

`dhw_delay_s = 90.0 s`, `tau_dhw_fast =
87.8440 s`, FIT-DHW entry RMSE = 0.7716 degC.
The final DHW timing is tuned on TUNE only; FIT-DHW is not part of the tuning
objective. Its RMSE at the final value is reported in Section 4.

### 3.4 Cooling constants from per-segment exponential fits

No radiator-only segment met the criteria (at least 20 minutes, fresh telemetry, at least 10 degC start excess over T_return), so `tau_rad_off` falls back to the legacy one-step estimate: 1897.3 s.

`tau_idle` = 8669.9 s (median, IQR
4382.4 s, N = 50
segments). The legacy one-step ratio gives 5927.9 s.
The room-sensor offset scan ranges over -5 to
+5 degC. The adopted offset is
+0.00 degC. The zero-offset residual is
24329.2; the best value anywhere in the scan
gives 22933.4, and the residual of the adopted value
is 24329.2. The scan optimum sits on the grid boundary, so it is treated as absorbing model mismatch rather than sensor bias, and the offset is kept at zero.

### 3.5 Hydraulic correlations

Settled Regime A (stable for at least ten minutes, mixer state unchanged for at
least five minutes, N = 20,730):

| Correlation | Settled | Full FIT window |
|---|---|---|
| `r(mixer_state, T_incoming)` | +0.7759 | +0.7466 |
| `r(mixer_state, dT_hx)` | -0.3540 | -0.4046 |
| `r(T_boiler, T_incoming)` | +0.7612 | n/a |

The no-material-linear-association claim is limited to `dT_hx`. The mixer state
is materially associated with `T_incoming` on the same settled samples, so the
claim is not extended to the outlet temperature.

## 4. TUNE results and parameters

The tuning objective is the equal-weight mean target-window RMSE over
TUNE-1..TUNE-3. DHW timing is tuned on TUNE only. `tau_rad_off` and `tau_idle`
stay at their FIT values.

### 4.1 Cold-start front

`L_cold_start = 100.0 s`, `tau_cold_front =
10.0 s`, fitted on all TUNE cold starts over the
measured transient length (30 samples). Cold-window RMSE =
1.0202 degC. Measured transient lengths: TUNE-1 v5 190 s, TUNE-1 v6.2 0 s, TUNE-3 v5 130 s, TUNE-3 v6.2 0 s.
`tau_cold_front` is constrained to at least one DT sample.

### 4.2 Coordinate search

The coordinate search ran 2 passes; the accepted moves are listed
below.

| Pass | Parameter | Value | Objective | TUNE-1 | TUNE-2 | TUNE-3 |
|---|---|---|---|---|---|---|
| 0 | initial | n/a | 1.806216 | 0.7881 | 2.5707 | 2.0598 |
| 1 | L_pump_off | 30.0 | 1.753636 | 0.8710 | 2.5707 | 1.8192 |
| 1 | L_burn_off | 0.0 | 1.751502 | 0.8677 | 2.5707 | 1.8161 |
| 1 | tau_burn_off | 3200.0 | 1.740206 | 0.8327 | 2.5707 | 1.8171 |
| 1 | tau_cutoff | 180.0 | 1.735085 | 0.7873 | 2.5709 | 1.8470 |
| 1 | dhw_delay_s | 135.0 | 1.169788 | 0.7873 | 0.8750 | 1.8470 |
| 2 | L_pump_off | 0.0 | 1.157937 | 0.8518 | 0.8750 | 1.7470 |

Boundary-limited parameters:
L_pump_off, L_burn_off (a
value at the zero floor means the hold is disabled). `L_burn_off < DT`
disables the casing hold; the effective value is
`0.0` s.

### 4.3 Final TUNE metrics

| Episode | Samples | RMSE (degC) | MAE (degC) | R2 | Bias (degC) |
|---|---|---|---|---|---|
| TUNE-1 | 1753 | 0.852 | 0.340 | 0.9826 | 0.087 |
| TUNE-2 | 211 | 0.875 | 0.502 | 0.9977 | 0.136 |
| TUNE-3 | 2065 | 1.747 | 1.213 | 0.9766 | 0.613 |

TUNE-1 is in-sample for the cold-start parameters. All TUNE residuals are
optimisation diagnostics, not out-of-sample evidence.

### 4.4 Final parameters

| Parameter | Value | Provenance |
|---|---|---|
| `tau_hx` | 79.8211 | FIT-1 (shortened, input-only mask, fresh samples) |
| `eps_A` | 0.944963 | FIT-1 (shortened, input-only mask, fresh samples) |
| `eps_B1` | 0.923137 | FIT 1-24 Dec input-only ratio B1/A * eps_A |
| `eps_B2` | 0.906018 | FIT 1-24 Dec input-only ratio B2/A * eps_A |
| `eps_C` | 0.909044 | FIT 1-24 Dec input-only ratio C/A * eps_A |
| `dhw_delay_s` | 135.0 | TUNE grid search (moved from 90 to 135) |
| `tau_dhw_fast` | 87.8440 | TUNE grid search (stationary) |
| `post_dhw_delay_s` | 90.0 | TUNE grid search (stationary) |
| `L_pump_off` | 0.0 | TUNE grid search (moved from 70 to 0) |
| `L_burn_off` | 0.0 | TUNE grid search (moved from 30 to 0) |
| `tau_burn_off` | 3200.0 | TUNE grid search (moved from 2400 to 3200) |
| `tau_cutoff` | 180.0 | TUNE grid search (moved from 150 to 180) |
| `L_cold_start` | 100.0 | all-TUNE cold-start fit (100 s); stationary on the TUNE search |
| `tau_cold_front` | 10.0 | all-TUNE cold-start fit (10 s); stationary on the TUNE search |
| `tau_rad_off` | 1897.3 | FIT per-segment exponential fit (median) |
| `tau_idle` | 8669.9 | FIT per-segment exponential fit (median) |

FIT-DHW RMSE at the final parameter dict: 4.7096 degC.

## 5. Observed validation (previously seen)

| ID | Episode | Samples | Excluded | Max tinc age (s) | RMSE (degC) | MAE (degC) | P95 (degC) | R2 |
|---|---|---|---|---|---|---|---|---|
| VAL-1 | VAL-1 Regime A Heated Run 2 | 1801 | 0 | 166 | 0.860 | 0.554 | 1.491 | 0.7509 |
| VAL-2 | VAL-2 Regime B Combined Run 1 | 991 | 0 | 96 | 0.993 | 0.836 | 1.502 | 0.9454 |
| VAL-3 | VAL-3 Regime B Combined Run 2 | 931 | 0 | 194 | 0.786 | 0.720 | 1.186 | 0.9826 |
| VAL-4 | VAL-4 Hydraulic Transition A->B | 901 | 0 | 185 | 1.070 | 1.028 | 1.335 | 0.9628 |
| VAL-5 | VAL-5 Observed Holdout (seen in v6.2) | 4321 | 0 | 296 | 3.436 | 2.511 | 6.763 | 0.9647 |
| VAL-6 | VAL-6 Circulating DHW Divert + Return | 391 | 0 | 76 | 4.433 | 3.029 | 10.762 | 0.9203 |
| VAL-7 | VAL-7 DHW Priority Divert (Idle) | 181 | 0 | 205 | 0.396 | 0.374 | 0.505 | -0.1608 |

These episodes are reported for continuity but are not sign-off evidence.
Pooled RMSE is shown beside the mean RMSE, and per-episode R2 beside the pooled
R2, because pooling gives long low-variance episodes more weight: the
per-episode R2 range here is
-0.1608 to 0.9826.

## 6. Phase 0 holdout (evaluated once)

| ID | Episode | Scored samples | RMSE (degC) | MAE (degC) | R2 |
|---|---|---|---|---|---|
| VAL-8 | VAL-8 Untouched Holdout: Cold Start, Cutoffs & Final Shutdown + Circulating DHW | 4321 | 3.421 | 2.675 | 0.9600 |
| VAL-9 | VAL-9 Untouched Holdout: Cold Start, Cutoffs & Final Shutdown + Circulating DHW | 4321 | 3.687 | 2.100 | 0.9625 |

Space-heating suite (VAL-8, VAL-9,
excluding DHW-on samples and DHW plunge windows): mean RMSE =
3.6279 degC, pooled RMSE = 3.6283 degC,
mean MAE = 2.4377 degC, minimum per-episode R2 =
0.9597, pooled R2 = 0.9615.

DHW suite (VAL-9): plunge-window RMSE =
15.8523 degC over 180 samples, R2 =
-1.7756.

The gate table in Section 0.0 is the complete pass/fail outcome. No parameter
or structure was changed after this evaluation.

## 7. Five-way comparison

### 7.1 TUNE-1

| Model | Overall RMSE | N | Cold-start RMSE | N | Mid-run cutoffs RMSE | N | Final shutdown RMSE | N |
|---|---|---|---|---|---|---|---|---|
| v3 baseline (frozen v5 constants) | 2.428 | 1753 | 3.724 | 30 | 5.408 | 52 | 8.571 | 113 |
| v4 clamped (frozen v5 constants) | 1.200 | 1753 | 3.724 | 30 | 3.382 | 52 | 3.548 | 113 |
| v5 two-stage (frozen v5 constants) | 0.920 | 1753 | 3.724 | 30 | 2.819 | 52 | 2.259 | 113 |
| v6.3 architecture (frozen v5 constants + fitted cold front) | 0.792 | 1753 | 0.993 | 30 | 2.819 | 52 | 2.259 | 113 |
| v6.3 final (fitted v6.3 constants) | 0.852 | 1753 | 0.905 | 30 | 2.617 | 52 | 2.669 | 113 |

The cold-start window is sized from the measured transient, mid-run cutoffs
require a relight, and the final shutdown includes the run-down. TUNE-1 is
in-sample for the cold-start parameters. The v6.3 architecture row uses the
fitted cold-start values rather than literals.

### 7.2 TUNE-3

| Model | Overall RMSE | N | Cold-start RMSE | N | Mid-run cutoffs RMSE | N | Final shutdown RMSE | N |
|---|---|---|---|---|---|---|---|---|
| v3 baseline (frozen v5 constants) | 4.763 | 2065 | 2.739 | 30 | 1.251 | 41 | 14.303 | 61 |
| v4 clamped (frozen v5 constants) | 8.081 | 2065 | 2.739 | 30 | 1.329 | 41 | 2.330 | 61 |
| v5 two-stage (frozen v5 constants) | 7.740 | 2065 | 2.739 | 30 | 1.345 | 41 | 3.006 | 61 |
| v6.3 architecture (frozen v5 constants + fitted cold front) | 3.006 | 2065 | 1.063 | 30 | 1.331 | 41 | 3.006 | 61 |
| v6.3 final (fitted v6.3 constants) | 1.747 | 2065 | 1.124 | 30 | 1.580 | 41 | 2.441 | 61 |

The pure-architecture and combined changes are reported per window; the tables
above carry the sample count for every window so that a small-window RMSE is
not mistaken for an episode-level result.

## 8. Python/C++ parity and unit tests

| Parity item | Result |
|---|---|
| Compiler | `/usr/bin/g++` |
| Flags | `-O2 -std=c++17` |
| Rows per parameter set | 30,712 |
| Default parameter set max abs diff | `5.684e-14` degC |
| Perturbed parameter set max abs diff | `5.684e-14` degC |
| Tolerance | `< 1e-09` degC |
| Perturbed set | `L_cold_start=200`, `L_pump_off=0`, `tau_cold_front=20` |
| Cold-start ring length | 10 samples |

| Synthetic test | Result | Expected | Detail |
|---|---|---|---|
| UT1_LowLift | PASS | ~25.03 C | final=25.0300 C |
| UT2_PumpOffHoldPlunge | PASS | hold then <30 C | L_pump_off=0 -> no hold, final=20.00 C |
| UT3_BurnerOffPumpOn | PASS | 25-50 C (casing decay) | final=36.77 C |
| UT4_DHWEntryReturn | PASS | DHW <32 C, recovery >55 C | dhw_end=25.00 C, rec_end=67.52 C |
| UT5_ColdStartWarmup | PASS | flat <21 C then >45 C | flat=20.00 C, warm=53.07 C |
| UT6_SingleStepGlitch | PASS | 1-sample blip stays casing; 2-sample relight is active | glitch branch=casing_decay, relight branch=active_heating, active no-glitch=98/glitch=98/relight=99, sim[202]=41.38 C |
| UT7_LowLiftBranch2B | PASS | v6 cutoff branch; v5 active (must fail) | v6 cutoff=599, final=25.00 C; v5 active=599 |

UT6 exercises the single-step firing glitch and UT7 the low-lift Branch 2B; both
include the legacy prediction that must fail, so they are not vacuous.

## 9. Limitations and residual risks

- **Holdout outcome.** The frozen gates are FAIL. The gate margins above
  are the only acceptance statement in this report.
- **Observed VAL-5.** The observed holdout remains a residual diagnostic. Its
  v6.2 failure was attributed in Section 0.1 to the branches and windows shown
  there, not to a generic statement about the model.
- **DHW timing.** On VAL-6 the measured versus simulated entry/plunge comparison
  is measured entry delay 20 s versus simulated 130 s, measured plunge depth 27.75 degC versus simulated 36.94 degC. The DHW objective is TUNE-only, so this remains an observed
  residual.
- **Idle cooling and radiator-only stagnation.** `tau_idle` and `tau_rad_off`
  are single global constants fitted from per-segment exponential decays; the
  IQRs in Section 3.4 show the segment-to-segment spread.
- **Sensor resolution.** Raw resolutions measured in this run: mixer incoming
  0.0625 degC, mixer return 0.0625 degC,
  room 0.1 degC and boiler flow 0.1 degC.
  A quantised plateau is not evidence that the true temperature is constant.
- **Causal pump filters.** The pump states are debounced with a trailing
  majority, so a real pump transition is represented after the majority flips;
  the offline centred-filter comparison in earlier versions was diagnostic only
  and is not a causal result.

## 10. Reproduction

```bash
uv run python simulation/select_holdout_v63.py   # Phase 0 (already frozen)
uv run python simulation/diag_v63.py             # Phase 1 diagnostics
uv run python simulation/step2_hx_dynamics_v63.py
```

## 11. Artifacts

| Artifact | Purpose |
|---|---|
| `simulation/step2_params_v63.json` | v6.3 parameter and metric export |
| `simulation/step2_hx_dynamics_v63.hpp` | Generated C++ implementation |
| `simulation/step2_hx_parity_harness_v63.cpp` | Parity harness |
| `simulation/step2_hx_parity_input_v63.csv` | Row-level parity input |
| `simulation/step2_hx_parity_harness_v63` | Compiled harness |
| `simulation/step2_hx_dynamics_report_v63.md` | This report |
| `simulation/tune1_diagnostic_overlay_v63.png` | TUNE-1 diagnostic overlay |
| `simulation/holdout_v63.json` | Frozen Phase 0 holdout |
| `simulation/gates_v63.json` | Frozen gates |
| `simulation/freeze_v63.json` | Frozen hashes and commit |
| `simulation/diag_v63/` | Phase 1 diagnostics |
| `simulation/v63_notes.md` | Findings, decisions and audit mapping |
