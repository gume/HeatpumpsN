# v6.3 — Findings, decisions and audit-finding mapping

This file records the Phase 1 diagnostics, every decision taken with its
evidence, and a table mapping each audit finding (1–17 plus the minor report
items) to the v6.3 change or to “not fixed — reason”.

Frozen provenance (Phase 0):

| Item | Value |
|---|---|
| Phase 0 commit | `facafae` |
| `gates_v63.json` SHA-256 | `270ee734cb4b458ac8f0891e66048098c4ad8e9cb42745182360434a801efbbf` |
| `holdout_v63.json` SHA-256 | `2fb8386ed273ea6803a097a38bc03b9f810f62f347e0d0cf58c1cb613d88a4b2` |
| Selector git blob | `cd4401d6bf7d5d80c7f4094ee9ad828b6edb9a32` |

The holdout was selected from telemetry only before any model code changed:
`VAL-8 = 2026-03-16 00:00–12:00` and `VAL-9 = 2026-03-22 12:00–24:00`.
Both satisfy the frozen criteria (cold start, at least two relit cutoffs, final
shutdown, ≤ 2 % effective stale samples); `VAL-9` also contains circulating DHW.

---

## Run provenance (aborted attempts)

The pipeline was executed several times while the new script was being built.
None of the aborted attempts changed the holdout selection, the gates or the
frozen parameters of the definitive run:

1. The first execution stopped at the synthetic unit tests (the old UT6
   assertion was wrong), before the parameter freeze and before the holdout
   stage. It produced no artifacts.
2. The second execution was terminated during development, after the parameter
   freeze but before completion. Its block-buffered stdout was never read and
   it wrote no artifacts (the parameter JSON, report and plot are written only
   at the end), so no holdout value was observed or used. The frozen B/C
   policies, gates and parameters were unchanged afterwards.
3. The definitive run is the only execution whose output was inspected and is
   the only holdout result reported in the report and the parameter JSON.
4. One report regeneration followed the definitive run. It corrected the
   VAL-6 DHW *entry* diagnostic, which had been anchored on the ww falling
   edge at the end of the DHW phase instead of the rising entry edge. The edit
   touches only `dhw_details()` and the report sentence that consumes it; the
   simulator, identification, tuning objective, grids, gates and holdout
   scoring are unchanged. The regeneration reproduced the recorded holdout
   metrics, gate outcomes and parameter hash exactly (programmatic comparison
   excluding only the timestamp and the v6.2 attribution table), so the
   reported holdout result is the single evaluated result and was not used to
   change anything.

`main()` re-checks the `gates_v63.json` and `holdout_v63.json` SHA-256 values
against `freeze_v63.json` at the start of every execution and refuses to
evaluate the holdout if either file changed.

---

## Definitive result (reported as is)

The frozen gates **FAIL**. Space-heating holdout: mean episode RMSE 3.63 °C
(gate < 1.25), pooled RMSE 3.63 °C (gate < 1.50), mean MAE 2.44 °C
(gate < 0.85), minimum per-episode R² 0.960 (gate > 0.90). DHW plunge on
VAL-9: RMSE 15.85 °C (gate < 4.50), R² −1.78 (gate > 0.90). Five of the six
gates fail; only the per-episode R² gate passes. The result is reported without
any retuning, gate change or holdout-driven structure change.

Supporting checks in the same run: all seven synthetic unit tests pass,
Python/C++ parity is 5.7e-14 °C on both the default and the perturbed parameter
sets, the disjointness assertion (including 30-minute warm-ups) passes, and the
final parameter dict hashes to `bf9fe353e10a2de59fb58e5461e684967105cff14827a7d0c07b6ad0cae2b31d`.

The failure is concentrated in the same mechanisms Phase 1 identified on the
observed holdout (idle/cutoff and cold-start behaviour), not in the active
heating branch, and in the post-DHW plunge. No fix was attempted after seeing
it, because that would invalidate the holdout. Any future fix must be developed
on FIT/TUNE, frozen, and validated on a new untouched holdout.

---

## Phase 1 findings (v6.2 parameters, no model changes)

All numbers below are from `simulation/diag_v63/`.

### 1.1 VAL-5 branch and window breakdown

- The largest branch residuals were `idle` (RMSE 4.97 °C, bias −4.56 °C),
  `circ_unheated_cutoff` (RMSE 3.80 °C, bias −1.95 °C) and `stale_hold`
  (RMSE 3.32 °C) on 346 stale samples. Active heating was 1.81 °C.
- The detected windows were a cold start (RMSE 4.26 °C) and five mid-run
  cutoffs (0.51–1.87 °C); the final shutdown was 1.14 °C. The cold-start and
  idle/cutoff phases, not the active-heating branch, dominate the failure.

### 1.2 Stale-hold cause

- VAL-5's 346 stale samples had fresh `tb`, `tinc`, `tret` and `p3`; every one
  violated the burner age (`bp_age_s > 14400 s`) and 327 also the pump
  modulation age. The cause is event-driven reporting, not a gateway dropout.
- Raw reporting intervals confirm the mechanism: `p1`/`p2`/`p3` have a median
  inter-sample interval of about 2 s, while `pmod` is reported every ~120 s
  when on and `bp` every ~20 s; the 99th percentiles exceed the loose
  threshold. Pump powers are therefore event-like signals, not analog ones.

### 1.3 Sensor quantisation

- `mixer_incoming_temperature` and `mixer_return_temperature`: 0.0625 °C.
- Room sensor: 0.1 °C. Boiler flow: 0.1 °C.
- Share of unchanged consecutive 1-minute samples: 2.6 % (incoming),
  2.4 % (return), 1.0 % (room), 8.5 % (boiler). Filtering on `d1 < d0` (the
  legacy one-step estimator) therefore discards quantised no-change steps and
  biases the time constant.

### 1.4 Split overlap (pre-fix)

- `FIT-1` and `VAL-4` overlapped by 60 minutes once warm-ups are included
  (FIT-1 ended exactly at VAL-4's start).
- `FIT-DHW` lies inside the 1–24 Dec FIT window; this is a nested FIT
  sub-window, not a cross-partition leak. v6.3 excludes its rows from the FIT
  ratio estimator and documents the nesting.
- No other overlap was found.

### 1.5 FIT-DHW side effect

- FIT-DHW RMSE at `dhw_delay_s = 135 s` was 4.69 °C, versus 1.31 °C at 90 s.
  The v6.2 DHW timing (135 s) was therefore worse on the FIT-DHW episode; the
  claim that FIT-DHW was considered in the objective was false.

### 1.6 TUNE-1 late window

- The 1820–1896 window has no burner-on sample and no relight afterwards; it is
  a run-down, not a mid-run cutoff. It is now classified as part of the final
  shutdown.

### 1.7 Cold-start transient length

- TUNE-1: v5 |error| > 1 °C for 190 s; v6.2 for 0 s at the detected window.
- VAL-5: both models are already within 1 °C at the detected window start, but
  the late cold-start RMSE is 7.2 °C, i.e. the window start is too late or too
  short to expose the error.
- Decision: size the cold-start fit and report windows from the measured
  transient (maximum over TUNE cold starts, both reference models), with a
  30-sample floor, instead of the previous seven samples.

### 1.8 Cutoff-dip mechanism

- In TUNE-1 and TUNE-3, the measured `T_incoming` dip at burner-off does **not**
  coincide with a drop in `pump_mod` while `T_boiler` rises; the boiler also
  falls at every event. The proposed `pmod`-scaled HX approach is therefore not
  supported by the diagnostic, so no structural variant was added (Phase 4.5
  is conditional on this confirmation).

---

## Decisions

1. **New holdout and gates.** Phase 0 files were committed before any model
   change; the selector uses telemetry only and the criteria are frozen in
   `holdout_v63.json`. The gates were chosen from HIL accuracy requirements
   before the holdout was evaluated.
2. **VAL-5 relabelled** as “observed holdout (seen in v6.2)”; it is reported but
   excluded from sign-off.
3. **FIT-1 shortened** to `2026-01-14 04:00`, removing the VAL-4 overlap while
   leaving a full 30-minute gap to VAL-4's warm-up. `tau_hx` and `eps_A` were
   re-fitted on the shortened window (input-only mask, fresh samples).
4. **FIT-DHW carved out of the FIT ratio window** so that no two FIT estimators
   share rows; the nesting is documented.
5. **Event-style staleness.** `bp`, `pmod`, `p1`, `p2`, `p3` are stale only when
   their age exceeds 14 400 s **and** `tb_age_s` exceeds 900 s. This removes the
   346 VAL-5 false holds while still holding on real gateway dropouts. The
   Python and C++ implementations are identical.
6. **Segment exponential fits.** `tau_idle` and `tau_rad_off` are fitted per
   contiguous segment (≥ 20 min, fresh telemetry, ≥ 10 °C start excess) by
   least squares over all samples, including quantised plateaus. The legacy
   one-step estimate is retained in the diagnostics. `tau_idle` = median of
   50 segments (IQR reported in the generated report). No radiator-only
   segment has ≥ 10 °C excess over `T_return`, so `tau_rad_off` falls back to
   the legacy estimate and the report states this.
7. **Room-sensor offset.** The offset scan moved monotonically to its grid
   boundary, which indicates model mismatch rather than a sensor bias, so the
   offset is **not adopted** and the zero offset is retained.
8. **Input-only effectiveness.** The mask uses `bp > 5`, `tb − tret > 10`,
   5-minute pump-state stability, no DHW and fresh telemetry. The same mask is
   used in the FIT-1 loss. Old and new ratios are reported side by side.
9. **Hydraulic claim.** The settled filter requires a 10-minute stable regime
   and a 5-minute unchanged mixer state. The report prints both the settled and
   full-FIT-window correlations and restricts the “no material linear
   association” claim to `ΔT_hx`; `r(mixer_state, T_incoming)` is reported as a
   material association.
10. **Grids and boundaries.** `L_pump_off`, `L_burn_off` and `L_cold_start`
    start at 0 (0 means no hold / no casing hold / minimum ring), grids are
    extended automatically when a selected value lands on a boundary, and any
    remaining boundary-limited parameter is reported. The cold-start fit did
    not sit on the extended lower boundary after the grid was widened to
    0..320 s, so the 100 s value is an interior optimum. `tau_cold_front` is
    constrained to ≥ DT, so it cannot become a pure delay.
11. **Cold-start fit.** All TUNE cold starts are used, over the measured
    transient length; the fit reports the RMSE and the parameters are also
    candidates in the coordinate search.
12. **Window detection.** Mid-run cutoffs require a burner relight; the
    remaining burner-off phases are run-downs folded into the final shutdown.
13. **No `pmod` structural variant.** Phase 1.8 did not confirm the mechanism
    (the dip coincides with a falling boiler temperature, not a pump-modulation
    drop while the boiler rises), so the optional variant was not added. This
    is a deliberate “not fixed — not supported by data” decision.
14. **DHW objective.** Tuning remains TUNE-only; the report no longer claims
    FIT-DHW was in the objective and instead reports the FIT-DHW RMSE at the
    final value.
15. **Parity and tests.** `n_cold` is clamped to 32 in Python, the C++
    `CausalMajority` is a template over `MAX_WINDOW`, the harness prints
    `%.15g`, and parity is checked on the default and a perturbed parameter set
    (`L_cold_start = 200`, `L_pump_off = 0`, `tau_cold_front = 20`) below
    `1e-9 °C`. UT2 is strict, and UT6/UT7 cover the single-step glitch and
    Branch 2B.
16. **Evaluation order.** `main()` runs FIT → TUNE → unit tests → parameter
    hash freeze → observed VAL → holdout once → five-way/parity/export/report.
    It refuses to evaluate the holdout if either frozen file hash differs.

---

## Audit-finding mapping

| # | Audit finding | v6.3 change |
|---|---|---|
| 1 | VAL-5 (2026-02-26) was presented as untouched although its v6.2 result had been seen | Relabelled “observed holdout (seen in v6.2)”; excluded from sign-off; reported separately |
| 2 | New untouched holdout must be selected from telemetry before model changes | `select_holdout_v63.py` + `holdout_v63.json`; VAL-8/VAL-9 selected and committed before any model edit |
| 3 | Gates must be frozen, hashed and committed before evaluation | `gates_v63.json` + `freeze_v63.json`; SHA-256 recorded and checked in `main()` |
| 4 | FIT-1 and VAL-4 overlapped, including VAL-4's warm-up | FIT-1 ends at `2026-01-14 04:00`; `tau_hx`/`eps_A` re-fitted; disjointness assertion in `main()` |
| 5 | FIT episodes loaded with inconsistent warm-up settings | FIT-1, FIT-DHW and the parity episode map all use `warmup_min=0` |
| 6 | FIT identification ignored telemetry staleness | `freshness_mask()` added to `load_fit_1m` consumers; stale FIT samples excluded from every estimator |
| 7 | `tau_idle`/`tau_rad_off` used a biased one-step ratio that discarded quantised no-change steps | Per-segment exponential least squares over all samples; median/IQR/N reported; legacy estimator kept for comparison |
| 8 | Idle reference used the room sensor directly | Offset scan `T_amb = T_room + δ`; adopted only for an interior optimum that improves the FIT residual (here: not adopted, boundary optimum) |
| 9 | Effectiveness selected on the output (`tinc >= 40`, `tinc − tret >= 10`) | Input-only mask (`bp > 5`, `tb − tret > 10`, 5-min stable pumps, no DHW); same mask in the FIT-1 loss; old/new reported |
| 10 | Hydraulic claim rested on a literal `r = −0.0089` and overclaimed | Full-season and settled correlations computed in code; claim limited to `ΔT_hx`; `r(mixer_state, T_incoming)` reported |
| 11 | Staleness guard treated event-driven pump powers as analog signals | Event-style rule for `bp`, `pmod`, `p1`, `p2`, `p3`; mirrored in C++; stale counts reported |
| 12 | `L_pump_off`/`L_burn_off` grids had no zero floor and could end on a boundary | Grids start at 0 and extend automatically; `L_burn_off <= DT` disables the casing hold; boundary-limited values reported |
| 13 | `tau_cold_front < DT` acted as a pure delay; cold fit used 7 samples on TUNE-1 only | `tau_cold_front >= DT`; fit window from the measured TUNE transient; all TUNE cold starts used |
| 14 | Window detection labelled run-downs as cutoffs and cold windows ended at 28 °C | Cutoffs require a relight; run-downs folded into the final shutdown; cold window sized from the measured transient |
| 15 | Optional `pmod` flow structure was proposed without confirmation | Phase 1.8 did not confirm the mechanism; variant not added (documented, not silently ignored) |
| 16 | DHW timing report claimed FIT-DHW was considered although the objective was TUNE-only | Objective stated as TUNE-only; FIT-DHW RMSE at the final value reported in §4.4 |
| 17 | Code/parity: unclamped Python `n_cold`, fixed C++ majority buffer, single parameter set, `%.10f`, missing UT coverage, lax UT2 | `n_cold <= 32` with fit-time assertion; `CausalMajority<MAX_WINDOW>`; two parity sets below `1e-9 °C`; `%.15g`; UT6/UT7 added; UT2 strict |
| m1 | Split described as “pre-registered” for previously observed episodes | VAL-1..4/6/7 labelled “previously observed”; only the Phase 0 holdout is untouched |
| m2 | Gate label “40 °C plunge RMSE” was not a plunge window | Explicit `ww 1→0` plunge windows (prior on ≥ 600 s, 600 s long); gate hash and margins printed; < 1 % margins flagged marginal |
| m3 | §4.2 prose was hard-coded | Move list and prose generated from `tune["history"]`; FIT-DHW claim removed |
| m4 | §3.6 hydraulic claim overstated | Corrected per finding 10 |
| m5 | §6 five-way used literal architecture parameters and only TUNE-1 | Architecture row uses fitted cold-start values; sample counts per window; TUNE-1 in-sample statement; TUNE-3 table added |
| m6 | §9 claimed VAL-5 cold-start validation and had a hard-coded DHW sentence | Claim removed; VAL-6 measured vs simulated entry delay and plunge depth computed in-run |
| m7 | §0 attributed the VAL-5 failure generically | Attribution from the in-run per-branch/per-window breakdown and the stale-hold count |
| m8 | Pooled metrics hid per-episode behaviour | Pooled RMSE beside mean RMSE, per-episode R² beside pooled R², with the R² range printed |
| m9 | Plot legend said “v6.2 error”; run-downs were shaded as cutoffs | Legend “v6.3 error”; run-downs shaded separately; only scored windows shaded |
| m10 | No provenance freeze before holdout | Commit id, gate/holdout hashes and parameter-dict SHA-256 printed; holdout refused on hash mismatch |
