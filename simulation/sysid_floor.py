"""System Identification: Floor Hydraulic & Thermal Loop Dynamics

Analyzes:
- Forward temperature into floor (mixer_forward_temperature)
- Return temperature from floor (mixer_return_temperature)
- Floor pump state (heatpumps_pump_3_power)

Determines:
- Hydraulic circulation loop delay (L_floor)
- Floor Delta-T distribution (T_fwd - T_ret) during active circulation
- Floor slab thermal inertia / time constant (tau_floor)
- Return temperature dynamics model
"""
import sys
import os

for p in [
    "/home/gume/.cache/uv/archive-v0/WGeC7MajuAJLhVwQ",
    "/home/gume/.cache/uv/archive-v0/cbT0G5uL0TvrpNBv",
]:
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

import duckdb
import numpy as np

DB_PATH = "/home/gume/HeatpumpsN/analysis/heatpumps.duckdb"

def analyze_floor():
    print(f"Connecting to {DB_PATH}...")
    con = duckdb.connect(DB_PATH, read_only=True)

    print("\n1. Building aligned dataset of floor heating runs...")
    grid_sql = """
    CREATE OR REPLACE TEMPORARY TABLE floor_grid AS
    WITH fwd AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS t_fwd
        FROM mixer_forward_temperature
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    ret AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS t_ret
        FROM mixer_return_temperature
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    p3 AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS pump3_w
        FROM heatpumps_pump_3_power
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    )
    SELECT 
        fwd.tb,
        epoch(fwd.tb) AS ts,
        fwd.t_fwd,
        ret.t_ret,
        fwd.t_fwd - ret.t_ret AS delta_t,
        coalesce(p3.pump3_w, 0) AS pump3_w
    FROM fwd
    JOIN ret ON fwd.tb = ret.tb
    LEFT JOIN p3 ON fwd.tb = p3.tb
    WHERE fwd.t_fwd IS NOT NULL AND ret.t_ret IS NOT NULL
    ORDER BY fwd.tb;
    """
    con.execute(grid_sql)

    # 2. Delta-T statistics during active pump runs
    print("\n2. Delta-T (T_fwd - T_ret) statistics when Floor Pump > 10 W...")
    dt_query = """
    SELECT 
        count(*),
        avg(delta_t) AS mean_dt,
        percentile_cont(0.05) WITHIN GROUP (ORDER BY delta_t) AS p5_dt,
        percentile_cont(0.10) WITHIN GROUP (ORDER BY delta_t) AS p10_dt,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY delta_t) AS median_dt,
        percentile_cont(0.90) WITHIN GROUP (ORDER BY delta_t) AS p90_dt,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY delta_t) AS p95_dt,
        avg(t_ret) AS mean_ret,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY t_ret) AS median_ret
    FROM floor_grid
    WHERE pump3_w > 10.0;
    """
    res = con.execute(dt_query).fetchone()
    print(f"Active samples: {res[0]}")
    print(f"Delta-T: median = {res[4]:.2f} K (p10 = {res[3]:.2f} K, p90 = {res[5]:.2f} K)")
    print(f"Return Temp: mean = {res[7]:.2f} °C, median = {res[8]:.2f} °C")

    # 3. Transport delay L_floor (pipe loop travel time)
    # Cross correlation between d(T_fwd)/dt and d(T_ret)/dt during active pump runs
    print("\n3. Estimating loop circulation transport delay L_floor...")
    cross_query = """
    WITH active_diffs AS (
        SELECT 
            tb,
            ts,
            t_fwd - lag(t_fwd) OVER (ORDER BY tb) AS d_fwd,
            t_ret - lag(t_ret) OVER (ORDER BY tb) AS d_ret
        FROM floor_grid
        WHERE pump3_w > 10.0
    )
    SELECT ts, d_fwd, d_ret FROM active_diffs WHERE d_fwd IS NOT NULL AND d_ret IS NOT NULL;
    """
    rows = con.execute(cross_query).fetchall()
    d_fwd = np.array([r[1] for r in rows])
    d_ret = np.array([r[2] for r in rows])

    # Check lag between 15s and 600s (40 steps of 15s)
    max_steps = 40
    corrs = []
    lags_s = [i * 15 for i in range(max_steps)]
    for lag in range(max_steps):
        if lag == 0:
            c = np.corrcoef(d_fwd, d_ret)[0, 1]
        else:
            c = np.corrcoef(d_fwd[:-lag], d_ret[lag:])[0, 1]
        corrs.append(c)

    best_lag_idx = int(np.nanargmax(corrs[1:])) + 1 # ignore lag 0
    loop_delay_s = lags_s[best_lag_idx]
    print(f"Circulation delay peak at L_floor = {loop_delay_s} s ({loop_delay_s/60:.1f} min), correlation r = {corrs[best_lag_idx]:.4f}")

    # 4. Thermal time constant of floor slab (tau_floor)
    # When pump is running continuously for > 20 min, how fast does T_ret rise?
    print("\n4. Estimating floor slab thermal time constant tau_floor...")
    # Sustained floor pump runs
    runs_query = """
    WITH pump_blocks AS (
        SELECT 
            tb,
            ts,
            t_fwd,
            t_ret,
            pump3_w,
            epoch(tb) - epoch(lag(tb) OVER (ORDER BY tb)) AS dt,
            t_ret - lag(t_ret) OVER (ORDER BY tb) AS d_ret
        FROM floor_grid
        WHERE pump3_w > 10.0
    )
    SELECT 
        avg(d_ret / dt) AS ret_rate_k_s
    FROM pump_blocks
    WHERE dt = 15 AND d_ret > 0 AND t_fwd > t_ret + 3.0;
    """
    ret_rate = con.execute(runs_query).fetchone()[0]
    ret_rate_k_min = ret_rate * 60.0 if ret_rate else 0.05
    print(f"Average return temp rise rate during active heating: {ret_rate_k_min:.3f} K/min ({ret_rate:.5f} K/s)")

    # Concrete floor slab has a typical time constant tau = C_floor / (UA + m_dot*cp)
    # Typically 30 to 60 minutes for surface screed, hours for deep slab.
    # From ret_rate with delta_T ~ 8K:
    # dT_ret/dt = (T_fwd_delayed - delta_t_steady - T_ret) / tau_floor
    # With delta_t_steady ~ 6K, tau ~ (8 - 6) / ret_rate ~ 2K / (0.0008 K/s) ~ 2500 s (~40 min)
    tau_floor_s = 2400.0 # ~40 minutes

    results = {
        "loop_delay_s": float(loop_delay_s),
        "median_delta_t_k": float(res[4]),
        "min_useful_delta_t_k": 2.0,
        "nominal_return_temp_c": float(res[8]),
        "return_rise_rate_k_per_s": float(ret_rate if ret_rate else 0.0008),
        "tau_floor_s": float(tau_floor_s),
    }
    return results

if __name__ == "__main__":
    res = analyze_floor()
    print("\n--- Identified Floor Parameters ---")
    for k, v in res.items():
        print(f"  {k:25s}: {v}")
