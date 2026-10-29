# Parameter extraction - summary of results

Final parameter set: `plant_parameters.json` (assembled by `collect_parameters.py` from the
step parameter files).  Revision history: `REVISIONS.md`.  Each step folder has a README and a
self-contained final report.

| Step | Final version | Acceptance (frozen gates) | Key result |
|---|---|---|---|
| 1 Data audit | v5.0 (received) | accepted | regimes, debounce, sensor characteristics |
| 2 HX / T_incoming | v7.1 | **FAIL** (official v7.0 holdout: 1/6 gates; space-heating pooled RMSE 2.09 vs 1.5 degC) | new physics: mixer-position flushing of the fork, HX heat store, DHW diverter memory; TUNE pooled RMSE 2.09 vs 4.84 for v6.3 |
| 3 Mixer static law | v1.0 | PASS (TUNE RMSE 0.82 degC) | hot fraction x(pos): leak 0.02, 0.20 at 50 %, 0.29 at 60 % |
| 4 Mixer dynamics | v2.0 | **FAIL** narrowly (holdout RMSE 1.60 / MAE 1.10 vs 1.5 / 1.0; timing gate not evaluable) | 40 s transport delay, 35 s sensor lag, radiator pumps reduce x by 21 %, 145 s actuator stroke confirmed |
| 5 Floor loop / slab / room | v1.0 | PASS (T_return RMSE 0.69, room night RMSE 0.36 degC) | one-node slab, tau_in 2.3 h, tau_out 2.3 h |
| 6 Integrated plant | v1.0 | PASS 8/8 | Python + C++ plant, parity 1e-14, coupled RMSE T_inc 2.07 / T_fwd 1.85 / T_ret 1.09 degC, legacy closed loop reproduces operating point |

Reproduce any step: `cd /home/gume/claude/HeatpumpsN && .venv/bin/python simulation/stepN/<script>`.
Data access is read-only (`analysis/heatpumps.duckdb`); `simulation/.cache/` holds regenerable caches.
