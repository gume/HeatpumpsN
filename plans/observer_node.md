## 9. Observer node responsibilities

1. **Ingest & store** logs (SQLite/InfluxDB + simple ingestion script).
2. **Identification tasks:**
   - Transport delay: cross-correlate pulse events with `T_mix_out` response onsets → delay as function of ΔT/flow conditions.
   - Gain & valve characteristic: regress ΔT_out per pulse vs. position and temperatures → build/refresh `f(pos, T_boiler, T_return)`.
   - Gradient statistics: distribution of dT/dt during responses → set safe gradient limits and pulse size caps.
3. **Parameter generation:** fit new model coefficients, **validate against held-out log data** (simulate: "would the new parameters have performed better last month?"), only then push.
4. **Supervision:** drift detection, anomaly reports, parameter versioning with rollback.
5. **Optional later stage:** offline RL / behavioral cloning in simulation using the identified plant model, evaluated against logs before any deployment.

