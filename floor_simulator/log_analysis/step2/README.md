# Step 2 - Primary heat exchanger dynamics (T_boiler -> T_incoming)

**Final:** v7.1 - report `step2_hx_dynamics_report_v71.md`, parameters `step2_params_v71.json`.

| File | Role |
|---|---|
| `step2_hx_dynamics_report_v71.md` | Final report |
| `step2_hx_dynamics_v71.py` | Final script (`--report-only` regenerates report + plots) |
| `hx_model_v71.py` | Final model, basis of the Step 6 plant |
| `step2_params_v71.json` | Final parameters and metrics |
| `holdout_observed_v71.json`, `step2_v71_*.png` | Outputs of the final script |
| `select_holdout_v7.py`, `holdout_v7.json`, `gates_v7.json`, `freeze_v7.json` | Holdout, FIT/TUNE split and gates, frozen before modelling (inputs to Steps 2-6) |
| `holdout_result_v7.json` | Record of the single official holdout evaluation (read by the final report) |
| `step2_hx_dynamics_v63.py`, `step2_params_v63.json`, `holdout_v63.json` | v6.3 reference model, needed only for the comparison columns of the final report |
| `v7_notes.md` | Development log referenced by the report |

Older versions are in git history only.
