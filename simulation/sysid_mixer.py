"""System Identification: Mixer Blending Dynamics & Valve Characteristic

Analyzes:
- Valve position (mixer_state with forward fill)
- Hot inlet (mixer_incoming_temperature)
- Cold return (mixer_return_temperature)
- Mixed forward outlet (mixer_forward_temperature)
During active floor heating (heatpumps_pump_3_power > 10 W).

Determines:
- Transport delay / dead time (L_mix)
- Mixing time constant (tau_mix)
- Empirical valve characteristic curve x(pos) = (T_fwd - T_ret) / (T_inc - T_ret)
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

def analyze_mixer():
    print(f"Connecting to {DB_PATH}...")
    con = duckdb.connect(DB_PATH, read_only=True)

    print("\n1. Building continuous 15-second aligned dataset with forward-filled position...")
    grid_sql = """
    CREATE OR REPLACE TEMPORARY TABLE mixer_grid AS
    WITH time_grid AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb
        FROM mixer_forward_temperature
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    fwd AS (
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
    inc AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS t_inc
        FROM mixer_incoming_temperature
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    pos AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS pos_raw
        FROM mixer_state
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    p3 AS (
        SELECT time_bucket(INTERVAL '15 seconds', t) AS tb, avg(v) AS pump3_w
        FROM heatpumps_pump_3_power
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    filled AS (
        SELECT 
            g.tb,
            epoch(g.tb) AS ts,
            fwd.t_fwd,
            ret.t_ret,
            inc.t_inc,
            last_value(pos.pos_raw IGNORE NULLS) OVER (ORDER BY g.tb ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS pos,
            coalesce(p3.pump3_w, 0) AS pump3_w
        FROM time_grid g
        JOIN fwd ON g.tb = fwd.tb
        JOIN ret ON g.tb = ret.tb
        JOIN inc ON g.tb = inc.tb
        LEFT JOIN pos ON g.tb = pos.tb
        LEFT JOIN p3 ON g.tb = p3.tb
    )
    SELECT * FROM filled
    WHERE pump3_w > 10.0
      AND pos IS NOT NULL
      AND t_inc > t_ret + 10.0
      AND t_fwd >= t_ret - 0.5
      AND pos BETWEEN 0.0 AND 100.0
    ORDER BY tb;
    """
    con.execute(grid_sql)

    count = con.execute("SELECT count(*) FROM mixer_grid;").fetchone()[0]
    print(f"Loaded {count} aligned 15-second samples during active floor heating.")

    # 2. Dead time / transport delay identification via cross-correlation of derivatives
    print("\n2. Estimating transport delay L_mix (cross-correlation of d(pos)/dt vs d(T_fwd)/dt)...")
    lag_query = """
    WITH diffs AS (
        SELECT 
            tb,
            ts,
            pos - lag(pos) OVER (ORDER BY tb) AS d_pos,
            t_fwd - lag(t_fwd) OVER (ORDER BY tb) AS d_fwd
        FROM mixer_grid
    )
    SELECT ts, d_pos, d_fwd FROM diffs WHERE d_pos IS NOT NULL AND d_fwd IS NOT NULL;
    """
    diff_rows = con.execute(lag_query).fetchall()
    d_pos = np.array([r[1] for r in diff_rows])
    d_fwd = np.array([r[2] for r in diff_rows])

    # Check movement events
    max_lag_steps = 80  # up to 20 minutes (80 * 15s)
    corrs = []
    lags_s = [i * 15 for i in range(max_lag_steps)]
    for lag in range(max_lag_steps):
        if lag == 0:
            c = np.corrcoef(d_pos, d_fwd)[0, 1]
        else:
            c = np.corrcoef(d_pos[:-lag], d_fwd[lag:])[0, 1]
        corrs.append(c)

    best_lag_idx = int(np.nanargmax(corrs))
    delay_s = lags_s[best_lag_idx]
    print(f"Cross-correlation derivative peaks at lag = {delay_s} s ({delay_s/60:.1f} min), correlation r = {corrs[best_lag_idx]:.4f}")

    # 3. Empirical valve characteristic: x(pos) = (T_fwd - T_ret) / (T_inc - T_ret)
    # Using 120s lag (8 steps of 15s) per physical transport time
    lag_steps = max(1, best_lag_idx) if delay_s > 0 else 8 # 120s
    actual_lag_s = lag_steps * 15
    print(f"\n3. Evaluating valve characteristic x = (T_fwd - T_ret) / (T_inc - T_ret) with {actual_lag_s} s lag...")
    
    char_query = f"""
    WITH lagged AS (
        SELECT 
            tb,
            lag(pos, {lag_steps}) OVER (ORDER BY tb) AS pos_lag,
            t_fwd,
            t_ret,
            t_inc,
            (t_fwd - t_ret) / (t_inc - t_ret) AS hot_fraction
        FROM mixer_grid
    )
    SELECT 
        floor(pos_lag / 10.0) * 10 AS pos_bin,
        count(*) AS n,
        avg(pos_lag) AS avg_pos,
        avg(hot_fraction) AS avg_x,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY hot_fraction) AS p50_x,
        percentile_cont(0.10) WITHIN GROUP (ORDER BY hot_fraction) AS p10_x,
        percentile_cont(0.90) WITHIN GROUP (ORDER BY hot_fraction) AS p90_x
    FROM lagged
    WHERE pos_lag IS NOT NULL 
      AND hot_fraction BETWEEN 0.0 AND 1.0
    GROUP BY 1
    ORDER BY 1;
    """
    bins = con.execute(char_query).fetchall()
    print("Pos Bin (%) | Samples | Avg Pos | Median x | p10 - p90 x")
    print("-------------------------------------------------------")
    x_curve = []
    for b in bins:
        print(f" {b[0]:4.0f}-{b[0]+10:<4.0f} | {b[1]:7d} | {b[2]:7.1f} | {b[4]:8.3f} | {b[5]:.3f} - {b[6]:.3f}")
        x_curve.append((b[2] / 100.0, b[4]))

    pos_arr = np.array([p for p, x in x_curve])
    x_arr = np.array([x for p, x in x_curve])

    # Fit quadratic: x ~ c1 * pos + c2 * pos^2
    A = np.vstack([pos_arr, pos_arr**2]).T
    c1, c2 = np.linalg.lstsq(A, x_arr, rcond=None)[0]
    print(f"\nQuadratic fit: x(pos) = {c1:.4f} * pos + {c2:.4f} * pos^2 (pos in 0..1)")

    # 4. Mixing time constant tau_mix
    print("\n4. Estimating mixing time constant tau_mix...")
    dyn_query = f"""
    WITH sim_data AS (
        SELECT 
            tb,
            lag(pos, {lag_steps}) OVER (ORDER BY tb) / 100.0 AS u,
            t_fwd,
            t_ret,
            t_inc,
            epoch(tb) - epoch(lag(tb) OVER (ORDER BY tb)) AS dt,
            t_fwd - lag(t_fwd) OVER (ORDER BY tb) AS d_fwd
        FROM mixer_grid
    )
    SELECT 
        dt,
        d_fwd,
        t_fwd,
        ({c1} * u + {c2} * u * u) * t_inc + (1.0 - ({c1} * u + {c2} * u * u)) * t_ret AS t_target
    FROM sim_data
    WHERE u IS NOT NULL AND dt = 15;
    """
    rows = con.execute(dyn_query).fetchall()
    d_fwd_arr = np.array([r[1] for r in rows])
    t_fwd_arr = np.array([r[2] for r in rows])
    t_tgt_arr = np.array([r[3] for r in rows])

    error = t_tgt_arr - t_fwd_arr
    dt = 15.0
    deriv = d_fwd_arr / dt

    mask = (np.abs(deriv) > 0.002) & (np.abs(error) > 0.5)
    if np.sum(mask) > 100:
        tau_s = float(np.dot(error[mask], deriv[mask]) / np.dot(deriv[mask], deriv[mask]))
        tau_s = max(30.0, min(tau_s, 300.0))
    else:
        tau_s = 140.0
    print(f"Identified mixing time constant tau_mix = {tau_s:.1f} s ({tau_s/60:.2f} min)")

    results = {
        "dead_time_s": float(actual_lag_s),
        "tau_mix_s": float(tau_s),
        "valve_curve_c1": float(c1),
        "valve_curve_c2": float(c2),
        "valve_bins": [(float(b[0]), float(b[4])) for b in bins],
    }
    return results

if __name__ == "__main__":
    res = analyze_mixer()
    print("\n--- Identified Mixer Parameters ---")
    for k, v in res.items():
        if k != "valve_bins":
            print(f"  {k:20s}: {v}")
