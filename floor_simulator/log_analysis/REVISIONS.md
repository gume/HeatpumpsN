# Report revision log

One entry per report version, newest first within each step.  Each report is
self-contained; this log only lists what changed and why.  Superseded files
that were removed during step cleanup (all older report/script/parameter versions) remain available in the git history
(initial import commit `7ec6d4a` holds the received v6/v6.3 state).

## Step 1 - Database audit, sensor quality and regime segmentation

| Version | Date | Status | Changes |
|---|---|---|---|
| v5.0 | 2026-10-28 | Accepted (received state) | Canonical 5-min pump debounce, regime reconciliation, zero-state partitioning, mixer concordance.  Not modified in this work. |

## Step 2 - Primary heat exchanger dynamics (T_boiler -> T_incoming)

| Version | Date | Status | Changes |
|---|---|---|---|
| v7.1 | 2026-10-29 | **Final** (recommended parameters; acceptance: v7.0 holdout FAIL is the official result) | Audit fixes of v7.0: (1) boiler-telemetry outages (T_boiler age > 900 s) now hold the model, re-anchor afterwards and are excluded (+30 min) from identification and scoring; (2) pre-declared parsimony rule removes structural elements with < 0.01 degC TUNE benefit (removed: diverter travel time, primary pipe delay); (3) report prose generated conditionally on fitted values (radiator flushing not supported, R-loop constants non-identifiable, single-cell pipe).  HO-1..HO-3 relabelled observed.  Report-only regeneration mode added. |
| v7.0 | 2026-10-29 | Superseded; holds the official holdout evaluation (FAIL, 1/6 gates) | New model structure: secondary-flow (mixer-position) flushing, HX heat store, latent radiator loop, DHW/CH diverter memory and travel, primary-pipe delay, sensor lag.  Global simulation-error identification on 156 FIT half-day blocks; 156 TUNE blocks; new untouched holdout HO-1..3 and gates frozen before modelling (`freeze_v7.json`).  Warm-ups inside blocks (strict disjointness).  DHW gate redefined before evaluation.  Python only; C++ deferred to Phase 6. |
| v6.3 | 2026-10-29 | Superseded (holdout FAIL) | Received state: audit fixes of v6.2.  Kept as comparison reference (`step2_hx_dynamics_v63.py`, `step2_params_v63.json`). |
| v6 / v6.2 | before | Superseded | Received state. |

## Step 3 - 3-way mixer static mixing characteristic

| Version | Date | Status | Changes |
|---|---|---|---|
| v1.0 | 2026-10-29 | **Final** (TUNE gates PASS: RMSE 0.82, bias -0.01, P95 1.51 degC) | First version.  Monotone piecewise-linear hot fraction x(pos) with Huber IRLS and smoothness penalty; structure by FIT week-parity CV (radiator offset and drift terms not selected); actuation/position-fault day rule (closed valve with x > 0.15) frozen before TUNE.  During the audit the report text was corrected three times before finalisation (fault-day documentation, smoothing description, withdrawal of an unsupported "saturation above 60 %" claim); every regeneration reproduced the identical TUNE numbers. |

## Step 4 - Mixer actuator, transport delay and forward-sensor dynamics

| Version | Date | Status | Changes |
|---|---|---|---|
| v2.0 | 2026-10-29 | **Final** (holdout HO-1..3 FAIL: RMSE 1.60 / MAE 1.10 vs 1.5 / 1.0; timing gate not evaluable) | After the v1 TUNE failure, FIT diagnostics showed 74 % of the squared error above 60 % valve position.  Added candidates: law re-estimated above 60 % (C5) and a radiator-pump multiplier on x (C6, selected: 0.793, CV RMSE 1.34 -> 1.13).  Delay 40 s, sensor lag 35 s.  Tested on HO-1..3 (untouched for T_forward); TUNE reported as observed (RMSE 1.28, MAE 0.74).  Plot legend corrected after the run (numbers verified identical). |
| v1.0 | 2026-10-29 | Superseded (TUNE FAIL: RMSE 1.545 vs 1.5; MAE PASS; timing not evaluable) | Step 3 law + plug delay (20 s) + sensor lag (54 s), incoming water temperature reconstructed with the Step 2 thermowell lag.  Frozen timing-gate event definition found to admit almost no events with the legacy PID. |

## Step 5 - Underfloor loop, slab and room dynamics

| Version | Date | Status | Changes |
|---|---|---|---|
| v1.0 | 2026-10-29 | **Final** (TUNE PASS: T_return RMSE 0.69 / MAE 0.55 / bias 0.15 degC; room night RMSE 0.36 degC) | One-node slab selected by FIT CV over two-node and transport-delay variants; room model on simulated slab.  Audit text fixes after the run (boundary-limited kappa, tau_in wording, layer interpretation, room identifiability); regenerated numbers verified identical. |

## Step 6 - Integrated HIL plant simulator and validation

| Version | Date | Status | Changes |
|---|---|---|---|
| v1.0 | 2026-10-29 | **Final** (8/8 gates PASS) | Unified plant (`plant_v1.py`) with exact component consistency (max diff 0), C++ header port (`plant_v1.hpp`, parity 1.4e-14 over 25,920 rows), coupled open-loop validation on outage-free TUNE blocks, closed-loop legacy-controller replica, boiler-emulator statistics.  Audit fixes after the first run: DHW-per-day statistic was doubled (fixed), closed-loop section now states that the replica does not reproduce the legacy valve slams. |
