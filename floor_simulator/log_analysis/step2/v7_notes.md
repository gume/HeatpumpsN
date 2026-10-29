# Step 2 v7 - working notes, decisions and exploration log

This file records how v7 was developed, in order, including everything that
could affect the trustworthiness of the holdout result.  The report
(`step2_hx_dynamics_report_v7.md`) is self-contained; this file is the audit
trail behind it.

## 1. Starting point

* v6.3 (received state) failed its frozen holdout (VAL-8/VAL-9): space-heating
  pooled RMSE 3.63 degC against a 1.5 degC gate, DHW plunge RMSE 15.9 degC.
  VAL-8/VAL-9 are therefore *observed* episodes from v7 on.
* Inspection of VAL-5 (observed since v6.2) showed the dominant failure: in
  long unheated periods v6.x relaxed T_incoming to T_return within minutes
  (`tau_cutoff` = 180 s while the floor pump ran), while the measured
  T_incoming decayed over hours.

## 2. Disclosure: aggregate diagnostics before Phase 0

Before the v7 holdout was selected, three *aggregate* diagnostics were run
over the whole heating season (all days pooled; no per-episode inspection of
any later holdout block):

1. HX ratio (T_inc - T_ret)/(T_b - T_ret) by mixer-position bin during steady
   firing (showed T_incoming stays near T_boiler even with the mixer closed);
2. derivative regression of T_incoming in unheated periods (R2 ~ 0, unusable);
3. trajectory fit of unheated segments (time constant to room temperature of
   roughly 8 000 - 13 000 s).

No parameter, threshold or structure from these runs is used directly; every
v7 parameter is re-identified on FIT blocks only.  All structural exploration
after Phase 0 used FIT blocks for fitting and TUNE blocks for comparison.

## 3. Phase 0 (committed before any v7 model code)

* `select_holdout_v7.py` (telemetry only) picked HO-1 (2025-12-07 12-24 h),
  HO-2 (2026-01-17 00-12 h), HO-3 (2026-02-27 00-12 h) from 12 qualifying
  12-h blocks (seed 20261029, one per month with candidates; all three contain
  circulating DHW).  March/April had no qualifying candidate outside the
  observed-episode buffers.
* `gates_v7.json`: thresholds identical to v6.3 (not lowered).  The DHW
  suite was redefined *before evaluation*: v6.3 scored [ww fall, +600 s] for
  every DHW edge, including idle-time DHW cycles where T_incoming is flat and
  R2 is meaningless.  v7 scores the full divert + recovery of *circulating*
  DHW events.
* Commit `ba5d968` holds both files; hashes in `freeze_v7.json`.

### 3.1 Known defect in the frozen selector: DST day

The selector built 12-h blocks as "local midnight + 12 h absolute".  On the
DST change day (2026-03-29) the second block therefore runs 13:00-01:00 and
overlaps the next day's first block by one hour.  The frozen JSON cannot be
changed.  `step2_hx_dynamics_v7.py` truncates every block at the next block
start, which shortens that one FIT block by one hour.  The disjointness
assertion found the defect.  Holdout blocks are not affected (Dec-Feb).

## 4. Structural exploration log (FIT fit, TUNE comparison)

All runs below fitted on every second FIT block (for speed) with the warm-up
*before* each block (an earlier convention, later replaced by the in-block
warm-up of the definitive script).  Numbers are pooled RMSE in degC on FIT /
TUNE.  They are exploratory and are not reproduced by the definitive script;
the in-run ablations (report Section 4.2) are the reproducible structural
evidence.

| # | Structure | FIT | TUNE | Finding that motivated the next step |
|---|---|---|---|---|
| S1 | single state, flow-gated HX, T_in = T_ret / latent loop | 4.66 | 4.58 | parameters on bounds; after a primary stop T_inc stays coupled to T_boiler in some periods, decoupled in others |
| S2 | + per-mode coupling, per-pump coupling, diverter memory (div := 1 on DHW, cleared by the next CH burner firing) | 4.04 | 4.01 | after DHW the coupling is about 0.05 (decoupled); otherwise bimodal |
| S3 | two states: sensor water T + heat store H | 3.24 | 3.25 | the remaining decoupled cases are separated by the mixer position after the stop (tree accuracy 0.93 on 345 FIT stop events) |
| S4 | OFF mode as flow-weighted mixing: floor flow = P3 * pos/100 at T_ret, radiator flows at a latent loop temperature R, parallel stagnant exchange with H | 2.52 | 2.59 | first 10 min of active heating carry about 25 % of the SSE; onset delays |
| S5 | + primary-pipe plug-flow ring | 2.47 | 2.52 | onset delay after DHW (median 100 s) is longer than after a plain pause (median 60 s) |
| S6 | + DHW->CH diverter travel time; diverter cleared only by a burner firing >= 30 s after ww falls | 2.45 | 2.51 | rise shapes are smoothed |
| S7 | + sensor (thermowell) first-order lag | 2.30 | 2.38 | -> frozen as v7 |

S7 is the structure in `hx_model_v7.py`.

## 5. Expectation recorded before the holdout evaluation

On TUNE the exploratory S7 fit gives a pooled RMSE of about 2.4 degC and a
mean MAE of about 1.25 degC.  The frozen space-heating gates are 1.5 degC
pooled RMSE and 0.85 degC mean MAE.  **The holdout is therefore expected to
FAIL** these gates.  The gates are not changed.  The residual budget shows
why: settled operation is reproduced at about the 1 degC level, while the
first minutes of every transition (primary start/stop, DHW) dominate the
squared error, and their timing is limited by the event-driven EMS telemetry
(burner and pump modulation are reported on change, with irregular latency).

## 6. Decisions

1. **Structure v7 = S7.**  Every element improved TUNE; the definitive run
   re-checks this with refitted ablations.
2. **Parameters identified once on all FIT blocks** by differential evolution
   (seed 7) and L-BFGS-B polish; TUNE is never fitted.
3. **Pump states** use the Step 1 centred 5-min majority filter for
   identification only; the HIL will feed relay states.
4. **No C++ export in Step 2.** Phase 6 builds one C++ plant model with a
   parity test.  This replaces the per-step header of v6.x.
5. **Holdout evaluated once.** `holdout_result_v7.json` stores the result
   with the parameter hash.  A later run with the same hash re-computes and
   asserts identical numbers; a different hash is refused.

## 7. v7.0 definitive run and audit (post-holdout)

* v7.0 ran once (commit `4546af2`).  Official holdout result: **FAIL**
  (space-heating mean RMSE 2.05, pooled 2.09, mean MAE 1.02 degC; min R2
  0.985 PASS; circulating-DHW pooled RMSE 9.0 degC, min R2 0.08).  Stored in
  `holdout_result_v7.json`; report `step2_hx_dynamics_report_v7.md`.
* Audit of the v7.0 report found:
  1. **Input outages not handled.** FIT/TUNE pooled RMSE (2.84 / 3.73) was
     far above the exploration values; the worst blocks (e.g. TUNE
     2026-02-12 12 h, FIT 2025-12-31 00 h) are EMS-gateway outages where
     T_boiler, burner and pump modulation stay frozen for hours while heating
     runs.  Two FIT outage blocks contribute about 40 % of the FIT
     objective, so the v7.0 parameters are biased.  The reported "18.36 %
     stale inputs" mixed real T_boiler outages (1.0 % of FIT, T_boiler age >
     900 s; raw report interval P99 = 310 s) with T_return de-duplication
     ages (17.7 %), which are benign.
  2. Report prose stated radiator-circuit flushing although q1 = q2 = 0 were
     fitted; prose must be conditional on the fitted values.
  3. Diverter travel and primary-pipe delay showed no TUNE benefit
     (ablation delta about 0), so they must not be presented as findings.
* These defects were found on FIT/TUNE data, independently of the holdout
  numbers.  Fixing them produces v7.1.  **HO-1..HO-3 are observed for
  v7.1** and cannot support a sign-off; no untouched holdout remains that
  meets the Phase 0 criteria (all 12 candidate blocks are in the split or
  already used), so the v7.0 FAIL stays the official holdout result.

## 8. v7.1 changes (decided before running v7.1)

1. Outage = T_boiler age > 900 s.  States held during the outage,
   re-anchored to measured T_incoming at its end; outage + 30 min not scored
   (identification and evaluation).
2. Pre-declared parsimony rule: after the full-structure fit, every element
   whose removal worsens TUNE pooled RMSE by less than 0.01 degC is removed
   and the rest is refitted on FIT.
3. Report prose generated conditionally on fitted values and ablation deltas.
