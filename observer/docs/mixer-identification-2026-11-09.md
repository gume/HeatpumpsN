# Mixer model re-identification and retry log check — 2026-11-09

Findings from the first re-identification on the controller's **exact slow
log** (not the published state values) and from the MPC anomaly/retry events.
The code state is `60e8ecd` (observer) and `7eb5eaa` (firmware); nothing was
pushed to the controller and no parameter set was sent.

## 1. Data

| What | Value |
|---|---|
| Slow log | `heatpumpsn/controller/log/slow#*`, every 10 s, exact model inputs |
| Recorded span | 2026-11-01 10:46 – 2026-11-09 15:40 (local) |
| Usable identification segments | 22, **39.57 h** (floor pump on, no DHW, no homing) |
| Identification window | `-14d` |
| Current parameter set | snapshot of `state/mpc/params/floor`, received 2026-11-09 15:22 |

Only the `floor` scenario has usable data; the radiator scenarios never ran in
the window. Every run is kept in
`<data dir>/data/identification.jsonl`; this run is also in the portal's
Parameters tab.

## 2. Re-identified model (free timing)

| Field | Current | Identified |
|---|---|---|
| `delay_s` | 40 | **10** |
| `tau_fwd_s` | 34.64 | **85** |
| `tau_inc_s` | 75.27 | 75 |
| `xk` at 0 / 25 / 50 / 52.5 / 55 / 57.5 / 60 / 65 / 70 / 80 / 90 % | 0.0197 / 0.0880 / 0.1957 / 0.2144 / 0.2435 / 0.2751 / 0.2891 / 0.2891 / 0.2891 / 0.5115 / 1 | **0 / 0 / 0.1725 / 0.2196 / 0.2349 / 0.2809 / 0.3325 / 0.3690 / 0.3690 / 0.5411 / 1** |

Held-out fit (the last 30 % of the recorded segments, never used for fitting):

| Model | Train RMSE | Hold-out RMSE |
|---|---|---|
| Current | 1.741 K | 1.346 K |
| Identified | 1.360 K | **0.801 K** |

The identified model predicts the recorded forward temperature clearly better.
That alone would create a proposal (the run did; id `edd054c886`).

## 3. Closed-loop backtest says no

The proposal was replayed with the controller's own MPC core on eight recorded
situations, on two plants: the plant as recorded and a plant generated from the
identified model. The portal verdict was **not recommended**.

| Plant | Model | RMSE | Overshoot | Travel %/h | Starts/h | Reversals/h | Median time to setpoint |
|---|---|---|---|---|---|---|---|
| current | current | 0.873 K | 2.56 K | 19.5 | 4.0 | 0.9 | 340 s |
| current | proposed | 0.826 K | 2.00 K | 25.0 | **8.1** | **3.8** | 380 s |
| identified | current | 2.473 K | 4.00 K | 49.8 | 9.4 | 4.6 | 110 s |
| identified | proposed | 0.910 K | 2.50 K | 22.2 | 4.7 | 2.2 | 120 s |

The proposal is worse on both plants: it needs roughly twice as many motor
starts and reversals per hour. Only 2 of 8 situations improve the RMSE by more
than 0.02 K. The lower overshoot is bought with more valve movement, which the
MPC's cost function penalises.

The same law with the **current timing kept** (delay 40 s, lags 34.64/75.27 s)
fits better than the current model on held-out data (0.966 K) and is
**neutral** in the backtest: on the current plant RMSE 0.873 -> 0.782 K, but
starts 4.0 -> 6.5/h, reversals 0.9 -> 3.0/h and median time to setpoint
340 -> 670 s. That is not a clear win either.

**Decision: keep the current parameter set. Do not approve `edd054c886`.**
Re-identify after more data at the new boiler setpoint (section 5).

## 4. Why the state fallback must not identify timing

The first re-identification (2026-11-03), on the published state values, found
delay 0 s, `tau_fwd` 75 s, `tau_inc` 0 s and a changed law. Those values are
10 s means of rounded/filtered signals: good enough for the law, too smeared
for delay and lags. On the exact slow log that timing is rejected by the
hold-out data. Since `60e8ecd`, a fallback to the state values fits only the
law and keeps the current delay and lags (`identify(fixed_timing=True)`); the
run record says the timing was kept.

## 5. Boiler hot-water setpoint 70 -> 60 °C (2026-11-08)

The owner lowered the boiler heating-water setpoint from 70 °C to 60 °C on
2026-11-08. Split at 2026-11-08 00:00 local:

| Data | Usable hours | Status | Held-out current / identified |
|---|---|---|---|
| before Nov 8 | 33.83 | current parameters are as good | 1.017 K / 1.269 K |
| after Nov 8 | 5.74 | proposal (not enough to trust) | 1.039 K / 0.625 K |

The post-change data hints at a different law (65/70 % knots near 0.45 instead
of 0.29, delay 10 s, `tau_fwd` 86.7 s), but 5.74 h is too little for a
proposal with a backtest. The pre-change data alone says the current model is
right. The apparent change is either the lower boiler temperature, the late
heating season, or both.

**Next:** collect several days at the 60 °C setpoint, then re-run the
identification on (a) all data and (b) post-change data only, and compare the
backtests. The setpoint itself is not in the database: see section 6.

## 6. Storage findings

* **Slow log is now stored.** 55 k samples, full span above; the collector
  hot-reloads `registry.yaml`, so no restart is needed after a Signals edit.
  This is the input the identification needs.
* **`ems-esp/boiler_data#heatingtemp` (the boiler heating-water setpoint) was
  `store: false`** in the data directory's `registry.yaml` (last modified
  2026-11-03), so the database had no history after 2026-11-01 16:49 and the
  exact 70 -> 60 time cannot be read back. The owner's store request did not
  reach the file; it is now set to `store: true` (2026-11-09 15:58, the
  collector reloaded it and stores 60 °C from then on). The split above
  therefore uses the owner's date, not the signal; the next setpoint change
  will be visible.
* The development InfluxDB was down for part of 2026-11-09. The collector kept
  retrying and backfilled when it came back; the slow log is complete to
  15:40. Expect gaps around such outages and check the span before a run.

## 7. Retry / stable-run log check (2026-11-01 .. 2026-11-09)

Events `1340` (anomaly), `1341` (fallback active) and `1342` (retry) plus the
floor pump and `stableS`:

* 8 overshoots; 6 came **3.5–4.9 min after the floor pump started** (the
  "first minutes" the owner described).
* 2 came late: 34.8 min and 37.3 min after the start. The second one
  (2026-11-09 01:13) was **87 s after an automatic retry** and is the event
  that the old latch turned into a permanent block.
* 9 automatic retries in the window; 8 survived the 2 h probation, 1 did not.
  So a retry is usually right, and one bad retry should not disable MPC for
  good.

The 20 min cautious-stable target is enough for the common heating-start
overshoot; it does **not** predict the retry failure (the loop had been quiet
for 52 h before that retry). That case is handled by the three-strike limit and
the portal's persistent warning plus **Unblock and resume MPC** button. The
old behaviour would have blocked after that single failure; the new one allows
three and then waits for the operator.

## 8. Actions

1. Keep the current `floor` parameters; do not approve `edd054c886`.
2. Optionally dismiss the proposal in Parameters if it is not wanted as a
   record.
3. `ems-esp/boiler_data#heatingtemp` is set to `store: true` now (done); its
   history starts 2026-11-09 15:59.
4. Re-identify after several days at 60 °C; compare all/post-change data and
   the backtest verdicts.
5. Nothing to upload for this finding: the slow-log/state fallback fix and the
   retry portal are already in the commits listed above.
