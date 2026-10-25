# Handover Document — Heat Pump / Mixer System Analysis Project


## 1. Context and Objective

A residential heating system is being upgraded. Two systems exist:

* **Old system (running, measured):** gas condensing boiler with an external motorized mixer valve (pulse-driven, no position feedback except an encoder) controlling a floor-heating circuit, plus radiator pumps. The control logic runs on an ESP32 board, defined with ESPHome yaml files; telemetry is persisted to InfluxDB 2.7 and visualized in Grafana. The old controller implements only part of the intended feature set.

* **New system (planned):** a controller specified in a control-logic document ("L2 spec", file `04_control_and_coordination_l2.md`). It defines mixer position estimation (dead reckoning + encoder, divergence warning, daily reset), DHW handling (mixer freeze, no false divergence alarms), ΔT-collapse protection, optional flush logic, and a coordination matrix between heater, pumps, and mixer.

**Goal of this project:** Use measurements from the old system to **derive/estimate the parameters the new controller needs**, and to understand real process dynamics. This is *reverse-engineering for design*, not auditing — the old system never ran the new logic, so its data cannot validate the new spec. Each L2 parameter must be classified as:

* **(a) measurable** directly from old data,

* **(b) partially estimable** (natural baseline from data + design margin chosen by the developer),

* **(c) pure design choice** (no data basis).


## 2. Terminology and System Operation

* **DR (Dead Reckoning), `pos_est_dr`:** the controller's internal estimate of mixer valve position, obtained by integrating open/close pulses (each pulse = fixed angular step) from a known reference. Expected to be the persisted series `mixer_state` (0–100, sw capped at 85 = pos_max). To be verified.

* **Encoder:** independent physical position measurement, `mixermeter_bmi160_mixer_value` (BMI160 rotary encoder). Ground truth for position.

* **Divergence:** difference between DR estimate and encoder reading; the new controller warns (`WARN_ENCODER_DIVERGED`) and resynchronizes when it exceeds `recalc_threshold`.

* **DHW:** domestic hot water. During DHW the boiler internally reroutes heated water; `boiler_heatingactive` stays ON, so DHW must be detected via `binary_sensor.boiler_wwcharging` or thermally (T_boiler high while mixer forward temperature cools).

* **Pumps (Radiator vs. Floor Heating):** Pump 1 and Pump 2 serve the radiator heating circuits and do not start together with the floor heating. Pump 3 drives the floor-heating circuit.

* **Active Heating vs. Unfulfilled Demand (Warning vs. Error):** Pump state or heating switch state alone is not a sufficient indicator of active heating. The controller may demand heat and switch on a pump even when the boiler itself is not set to its heating position (as is the case currently at the end of the non-heating season). Even if the heating switch is ON and a pump is running, no actual heating takes place in this state—this condition should be treated as a **warning** rather than a system **error**. The definitive ("perfect") indicator of active floor heating is a **rising forward temperature in the mixer (`mixer_forward_temperature`)**.


## 3. Data Access and Dataset Coverage

* **InfluxDB 2.x**, API at `[http://192.168.2.6:58086](http://192.168.2.6:58086)`, org `home_org`, bucket `home_assistant`, token `secret_token_heatpumps`.

* Query via HTTP `/api/v2/query` (Flux language, CSV output) — e.g. from Python with `requests` + pandas — or via CLI:
`docker exec influxdb influx query --raw --host http://localhost:58086 --org home_org --token secret_token_heatpumps '<flux>'` (use `--raw` to avoid output truncation; `--host http://localhost:58086` also works from outside the container).

* Query InfluxDB **directly**, not via Grafana, to get full native resolution (~12 s sampling).

* Timestamps are UTC-stored; local display in Grafana. Timezone must be handled consistently in scripts.

* Grafana dashboards already exist and work (an earlier dashboard-UID issue was resolved); they are for eyeballing only.

* **Dataset coverage & seasonality:** The database contains almost a full year of telemetry, but heating is not active continuously throughout the year. Currently, it is the end of the non-heating season, meaning the most recent data in the database is not heating-related (even if pumps run due to controller heat demand while the boiler is not set to heating mode). Consequently, locating the actual historical heating windows in the database is a required step before running heating-specific analyses.

* **Verified query pattern (important):** Filters on `entity_id` alone (tag-only, without pinning `_measurement`) returned empty results repeatedly; queries that pin **`_measurement` + `entity_id` + `_field == "value"**` work reliably. Always pin all three.

* **InfluxDB quirks encountered during export:** Some chunked queries returned header-only/empty responses even for ranges with data (server quirk with selective filters). Export code must tolerate empty chunks (skip, don't crash) and, as a safety net, fall back to a single full-range query, which worked in all probes.
* **Bulk export completed:** All 15 series have been exported from InfluxDB to per-entity CSV.gz files (`<entity_id>.csv.gz`, columns `t,v`) via `services/homeassistant/analysis/export_series.py` (chunked, 14-day windows, resumable: existing files are skipped). The tarball `ha_export.tar.gz` is the transfer artifact for the analysis machine. Further InfluxDB querying is no longer required for the core analyses; work continues on the exported files.

* **Room sensor d281 details:** `temperature_humidity_sensor_d281_temperature` (measurement `°C`) stores its reading in `_field == "value"` like the other numeric sensors, but additionally carries metadata fields `device_class_str`, `friendly_name_str`, `state_class_str` (each with the same row count as `value`) — these must be excluded via the `_field == "value"` filter to avoid garbage rows.


## 4. Available Series (verified inventory)

Numeric entities: measurement `%`, `W`, or `°C`, field `value`, tag `entity_id`. Binary sensors are their own measurements.

| entity_id | Measurement | Meaning |
| --- | --- | --- |
| `mixer_state` | % | Likely the DR position estimate, 0–100, capped in the old controller sw at 85 (**verify**) |
| `mixermeter_bmi160_mixer_value` | % | Encoder — physical valve position |
| `mixer_forward_temperature` | °C | T_mix_out — mixed water to floor circuit. **Rising values are the definitive indicator of active floor heating.** |
| `mixer_return_temperature` | °C | T_return_floor |
| `mixer_incoming_temperature` | °C | Return-side inlet, heat-exchanger secondary circuit — **use this** (not T_boiler) as return-side input for mixing/ff modeling |
| `boiler_curburnpow` | W | Burner power (>0 ⇒ heating) |
| `boiler_curflowtemp` | °C | T_boiler — high during DHW; separated from circuit by heat exchanger |
| `boiler_heatingpumpmod` | (likely %) | Boiler heating pump modulation |
| `binary_sensor.boiler_heatingactive` | — | Heating active (stays ON during DHW) |
| `binary_sensor.boiler_wwcharging` | — | **DHW indicator** |
| `heatpumps_pump_1_power` / `_2_` / `_3_` | W | Radiator pumps 1/2 (cycling) and pump 3 (floor heating). Pumps 1 & 2 do not start together with floor heating; pump operation alone does not guarantee active heating if the boiler is not in heating position. |
| `outside_temperature_last_value` | °C | Outside temperature |
| `temperature_humidity_sensor_d281_temperature` | °C | Room temperature (bonus for correlations). 33,512 rows (Dec 2025 → present). Extra `_field`s (`*_str`) exist and must be filtered out. |

**Exported dataset (all series, START = 2025-12-01):** Exact row counts per series are recorded in the export log; `temperature_humidity_sensor_d281_temperature` cross-checks at exactly 33,512 rows against an independent InfluxDB count query — export pipeline validated end-to-end.

**Not persisted in the old system:** `pos_est_dr` as a separate variable (likely = `mixer_state`), `divergence_reason`, `flush_state` (no flush installed). HA's own history exists but is coarser — prefer InfluxDB.


## 5. Analysis Plan (Python scripts, pandas + direct Flux queries)

Scripts live in `services/homeassistant/analysis/` (Jetson). All thresholds tuned after inspecting actual signal dynamics.

−1. **Load exported CSV.gz files into DuckDB (analysis machine):** Build a single DuckDB database from `ha_export.tar.gz` (one table per entity or one long table with `entity_id` column; parse `t` as TIMESTAMP with UTC timezone handling per §3). This DB is the input for all subsequent scripts — no further InfluxDB access needed. Verify row counts per entity against the export log during load.

0. **Locate active heating periods (seasonality & heating-window discovery):** Scan the ~1-year dataset to find when actual heating occurred, since recent data is from the end of the non-heating season. Because the controller can switch on a pump upon heat demand even when the boiler is not set to its heating position, pump state alone is insufficient to identify heating. Use **rising `mixer_forward_temperature**` as the definitive indicator of active floor heating (with Pump 3), track radiator heating (Pumps 1 and 2, which do not start together with floor heating) separately, and classify instances where a pump runs without temperature rise as a **warning** (boiler not in heating position) rather than an error.

1. **`00_verify_mixer_state.py` — semantics check (open):** raw sample of `mixer_state` + encoder; confirm DR hypothesis: changes only in discrete pulse-sized steps during movement, constant when idle, jump to fixed value at daily reset. Determines whether direct DR-vs-encoder comparison is possible. Must be evaluated on an identified active heating window rather than recent non-heating data.

2. **Encoder characterization:** noise floor, staleness (flatline stretches), jumps, sampling behavior → natural baseline for new divergence/staleness thresholds.

3. **DR vs encoder divergence statistics:** drift rate, divergence event frequency/magnitude in normal operation → sizes `recalc_threshold` and resync strategy. (Only meaningful once `mixer_state` semantics are confirmed.)

4. **Mixing thermodynamics:** regression of `T_forward` against position and inlet temps: `T_forward ≈ pos·ff_boiler·T_boiler + (1−pos)·ff_incoming·T_incoming` → calibrate `ff_boiler`, `ff_incoming`; measure response lag (position step → temperature response) → mixing dynamics time constants; achievable temperature span.

5. **DHW characterization:** windows via `wwcharging` / thermal signature; durations, frequency, temperature trajectories → DHW window handling and mixer-freeze rules for the new controller.

6. **Circuit dynamics:** ΔT distribution of floor circuit (`T_forward − T_return_floor`) → `min_useful_delta_t`; pump cycling patterns and runtimes → pump delays/pause durations (`pump_stop_after_heating_s`, `floor_pause_duration_s`). Account for the separate operation of radiator pumps (Pumps 1 and 2) versus the floor-heating pump (Pump 3).

7. **Coordination inventory:** classify all observed state combinations (heater/pumps/mixer/DHW); document which situations actually occur and which co-occurrences cause problems → informs the new coordination matrix. Explicitly include the unfulfilled-demand state (heating switch ON + pump running while boiler is not set to heating position, yielding no rise in `mixer_forward_temperature`), which should trigger a warning rather than an error.

8. **Exploratory:** correlations with outside/room temperature, seasonal load, boiler modulation patterns.


## 6. Key Findings / Decisions So Far

* Grafana dashboard provisioned and live; mixer/pumps/temperatures/boiler panels all show data.

* Measurements list confirmed via schema queries: measurements `%`, `W`, `°C`, `binary_sensor.boiler_heatingactive`, `binary_sensor.boiler_wwcharging`; fields `value` + metadata strings; tag `entity_id`.

* `mixer_state` is 0–100 sw capped at 85 — strong indication it is the DR estimate (pending verification).

* `boiler_heatingactive` does NOT go off during DHW → DHW detection via `wwcharging` or thermal signature.

* `T_incoming` (not `T_boiler`) is the correct return-side temperature for mixing calculations (heat exchanger between them, different circuits).

* No flush hardware → all flush-related L2 parameters out of scope.

* Consequence of old-vs-new split: safety/logic thresholds (e.g. `recalc_threshold`, `encoder_staleness_s`) can only get **natural baselines** from data; the actual values are design decisions.

* **Seasonality & non-heating recent data:** Although the database spans almost a full year, there is no heating all the time; because it is currently the end of the non-heating season, recent data is not heating-related.

* **Pump assignment & independence:** Pump 1 and Pump 2 drive radiator heating and do not start together with the floor heating; Pump 3 drives the floor heating.

* **Reliable heating detection & warning classification:** Pump operation and heating switch state alone do not guarantee active heating, as the controller can demand heat and turn on a pump even when the boiler is not set to its heating position (as happens right now). This state is considered a **warning**, not an error. The reliable indication of active heating is a **rising `mixer_forward_temperature**`.

* **Export pipeline proven:** All 15 series successfully exported to CSV.gz (per-entity files, UTC timestamps, columns `t,v`). Two robustness lessons baked into `export_series.py`: (1) always pin `_measurement` + `entity_id` + `_field` in Flux filters — tag-only filters silently returned empty; (2) tolerate header-only chunk responses and use a full-range query as fallback.
* **Query access pattern for future needs:** If new series ever need exporting, reuse `export_series.py` (add the entity to `ENTITIES` with its measurement); the script is resumable and skips already-exported files.
* **Analysis substrate decision:** Further analysis runs on the exported files (DuckDB on the analysis machine), not against live InfluxDB — decouples analysis from the Jetson/Home Assistant host.


## 7. Open Items / Next Steps

0. ~~Find active heating windows~~ — now to be computed **from the exported CSV files / DuckDB**, not via live Flux queries. Same logic as before: rising `mixer_forward_temperature` = active floor heating; radiator Pumps 1/2 tracked separately from Pump 3; pump-running-without-temperature-rise = warning (unfulfilled demand).

1. **Build the DuckDB database** from `ha_export.tar.gz` on the analysis machine (step −1 in §5), with per-entity row-count verification against the export log. This is the immediate next action.

2. Build the **parameter classification table** (measurable / baseline / design-choice) covering all L2 parameters — master checklist for the project.

3. Implement scripts 2–4 first (encoder characterization, divergence stats, thermodynamics) — highest data-driven value.

4. Tune all detection thresholds after first real-data inspection.

5. Deliverable: parameter table with values + confidence + source (data-derived / baseline+margin / design choice), feeding the new controller's configuration.

**How to resume:** Data inventory complete (§4) and full export finished — all 15 series as CSV.gz, transferred via `ha_export.tar.gz`. Next session starts on the analysis machine: (1) build DuckDB from the export, (2) locate active heating windows in the ~1-year data, (3) run the `mixer_state` DR-hypothesis verification on an identified heating window (gating step for scripts 3+), then proceed with encoder characterization and thermodynamics (§5 steps 2–4).