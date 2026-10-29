# Step 2 - Primary heat exchanger dynamics (T_boiler -> T_incoming)

**Final:** v7.1 - report `step2_hx_dynamics_report_v71.md`, parameters `step2_params_v71.json`.

| File | Role |
|---|---|
| `step2_hx_dynamics_report_v71.md` | Final, self-contained report |
| `step2_hx_dynamics_v71.py` | Final script (identification, ablations, evaluation, report; `--report-only` regenerates report + plots) |
| `hx_model_v71.py` | Final model (numba), basis of the Phase 6 C++ port |
| `step2_params_v71.json` | Final parameters and metrics |
| `holdout_observed_v71.json` | v7.1 scores on HO-1..3 (observed) |
| `step2_v71_*.png` | Overlays used by the report |
| `select_holdout_v7.py`, `holdout_v7.json`, `gates_v7.json`, `freeze_v7.json` | Phase 0: holdout, FIT/TUNE split and gates, frozen before modelling |
| `step2_hx_dynamics_report_v7.md`, `holdout_result_v7.json`, `step2_params_v7.json`, `step2_v7_*.png`, `step2_hx_dynamics_v7.py`, `hx_model_v7.py` | v7.0: the single official holdout evaluation (FAIL) |
| `v7_notes.md` | Exploration log, disclosures, audit findings, decisions |
| `step2_hx_dynamics_v63.py`, `step2_params_v63.json`, `holdout_v63.json` | v6.3 reference model (imported for comparisons only) |

Superseded v6/v6.3 files are in git history (commit `7ec6d4a`).
