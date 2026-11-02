---
title: Plant identification (system identification from recorded data)
description: Identifies dynamic parameters of a heating plant (delays, time constants, gains, mixing laws) from history, with frozen acceptance gates and a holdout - the method of floor_simulator/log_analysis.
tools: [resample_hold, debounce_binary, on_periods, fopdt_fit, pair_offset]
---
Goal: parameters of one part of the plant (e.g. valve -> supply sensor delay
and lag, supply -> return slab dynamics, heat-exchanger response) that a
simulator or a controller can use, with evidence that they generalise.

Method (learned in floor_simulator/log_analysis; follow it strictly):

1. **Scope one sub-system per run.** Say input(s), output and the physical
   model structure first (e.g. first order plus dead time).
2. **Data audit before modelling.** `data_coverage`, sample intervals,
   deadbands (values published only on change), outages. Never hold a value
   across an outage: `resample_hold` with a hold horizon (e.g. 180 s while
   active). Measurement artefacts are not physics: debounce on/off signals
   (`debounce_binary`), drop known-faulty periods (owner knowledge).
3. **Regimes.** Identify only in the regime the model is for (e.g. only the
   floor pump running, no DHW charging); `on_periods` gives the segments.
   Keep segments longer than the slowest time constant.
4. **Freeze the acceptance gates before fitting** (write them to
   `gates.json` in the workspace): e.g. holdout RMSE < 1.5 K, bias < 0.3 K.
   Split the segments by time into FIT, TUNE and an untouched HOLDOUT
   (e.g. alternate days, last days as holdout); never look at the holdout
   while choosing the structure.
5. **Fit** on FIT (`fopdt_fit` for first-order parts, own code for others,
   e.g. a piecewise-linear mixing law), choose structure on TUNE, then
   evaluate once on HOLDOUT by **free-run simulation**, not one-step
   prediction.
6. **Parsimony.** Remove a model element whose benefit on TUNE is below a
   threshold you declared beforehand.
7. **Report honestly.** PASS or FAIL against the frozen gates (a FAIL is a
   result, not something to tune away), parameters with units, plots of
   simulated vs measured on TUNE and HOLDOUT, the exact data selection, and
   every assumption. Verify every claim against the data, not against
   earlier notes. Save the parameters as JSON in the workspace.
8. If a reusable step emerges (a regime splitter, a mixing-law fit), propose
   it as a tool with `create_tool` (generic inputs, synthetic tests).
