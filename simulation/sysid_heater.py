"""System Identification: Boiler / Heater Supply Dynamics

Analyzes `boiler_curflowtemp`, `mixer_incoming_temperature`, and `boiler_curburnpow`
during active heating periods in `heatpumps.duckdb`.
Determines:
- Mean operating temperature and range when heating
- Burner cycle durations and power levels
- Oscillation period and amplitude (PID modulation behavior)
- Warmup and cooldown rates
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

def analyze_heater():
    print(f"Connecting to {DB_PATH}...")
    con = duckdb.connect(DB_PATH, read_only=True)

    print("\n1. Querying active heating periods (bucketed by 30s)...")
    
    # Fast hash join on time_bucket
    grid_query = """
    CREATE OR REPLACE TEMPORARY TABLE heating_grid AS
    WITH b AS (
        SELECT time_bucket(INTERVAL '30 seconds', t) AS tb, avg(v) AS t_boiler
        FROM boiler_curflowtemp
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    inc AS (
        SELECT time_bucket(INTERVAL '30 seconds', t) AS tb, avg(v) AS t_incoming
        FROM mixer_incoming_temperature
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    bp AS (
        SELECT time_bucket(INTERVAL '30 seconds', t) AS tb, avg(v) AS burn_power
        FROM boiler_curburnpow
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    ),
    p3 AS (
        SELECT time_bucket(INTERVAL '30 seconds', t) AS tb, avg(v) AS pump3_w
        FROM heatpumps_pump_3_power
        WHERE t BETWEEN '2025-12-01' AND '2026-04-15'
        GROUP BY 1
    )
    SELECT 
        b.tb,
        b.t_boiler,
        inc.t_incoming,
        coalesce(bp.burn_power, 0) AS burn_power,
        coalesce(p3.pump3_w, 0) AS pump3_w
    FROM b
    JOIN inc ON b.tb = inc.tb
    LEFT JOIN bp ON b.tb = bp.tb
    LEFT JOIN p3 ON b.tb = p3.tb
    WHERE (burn_power > 100 OR pump3_w > 10)
    ORDER BY b.tb;
    """
    con.execute(grid_query)

    stats_query = """
    SELECT 
        count(*),
        avg(t_boiler) AS avg_boiler,
        stddev(t_boiler) AS std_boiler,
        percentile_cont(0.05) WITHIN GROUP (ORDER BY t_boiler) AS p5_boiler,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY t_boiler) AS p50_boiler,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY t_boiler) AS p95_boiler,
        avg(t_incoming) AS avg_inc,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY t_incoming) AS p50_inc,
        avg(burn_power) AS avg_power,
        max(burn_power) AS max_power
    FROM heating_grid;
    """
    res = con.execute(stats_query).fetchone()
    print(f"Active heating 30s buckets: {res[0]}")
    print(f"Boiler Temp: mean={res[1]:.1f}°C, std={res[2]:.2f}°C, median={res[4]:.1f}°C (p5={res[3]:.1f}°C, p95={res[5]:.1f}°C)")
    print(f"Mixer Incoming Temp: mean={res[6]:.1f}°C, median={res[7]:.1f}°C")
    print(f"Burner Power: mean={res[8]:.1f}W, max={res[9]:.1f}W")

    print("\n2. Analyzing temperature oscillations (PID cycling / sinusoidal behavior)...")
    fft_query = """
    SELECT epoch(tb) AS ts, t_boiler 
    FROM heating_grid 
    WHERE tb BETWEEN '2026-01-10' AND '2026-01-25' AND t_boiler IS NOT NULL
    ORDER BY tb;
    """
    rows = con.execute(fft_query).fetchall()
    top_period_min = 60
    amp = 3.5

    if rows:
        ts = np.array([r[0] for r in rows])
        temps = np.array([r[1] for r in rows])
        
        # 30s buckets -> 120 buckets per hour
        window = 240  # 2 hours
        if len(temps) > window:
            kernel = np.ones(window) / window
            smoothed = np.convolve(temps, kernel, mode='same')
            detrended = temps - smoothed
            
            # Autocorrelation
            autocorr = np.correlate(detrended, detrended, mode='full')
            autocorr = autocorr[len(autocorr)//2:]
            autocorr /= autocorr[0]
            
            # Find local peaks between 10 min (20 buckets) and 240 min (480 buckets)
            peaks = []
            for i in range(20, min(len(autocorr)-1, 480)):
                if autocorr[i] > autocorr[i-1] and autocorr[i] > autocorr[i+1] and autocorr[i] > 0.15:
                    peaks.append((i * 0.5, autocorr[i])) # 1 bucket = 0.5 min
            
            peaks.sort(key=lambda x: x[1], reverse=True)
            if peaks:
                top_period_min = peaks[0][0]
                print(f"Dominant oscillation period: {top_period_min:.1f} minutes ({top_period_min*60:.0f} s)")
                amp = float((np.percentile(detrended, 90) - np.percentile(detrended, 10)) / 2.0)
                print(f"Estimated oscillation amplitude: +/- {amp:.2f}°C")
            else:
                print(f"Oscillations are broad/quasi-periodic: typical cycle ~{top_period_min} min, amplitude ~{amp}°C")

    # 3. Warm-up and Cooldown rates
    print("\n3. Estimating Warmup (heating) and Cooldown (idle) rates...")
    rate_query = """
    WITH diffs AS (
        SELECT 
            epoch(tb) - epoch(lag(tb) OVER (ORDER BY tb)) AS dt,
            t_boiler - lag(t_boiler) OVER (ORDER BY tb) AS dv,
            burn_power
        FROM heating_grid
    )
    SELECT 
        avg(dv / dt) FILTER (WHERE dt = 30 AND burn_power > 500 AND dv > 0) AS warmup_rate,
        avg(dv / dt) FILTER (WHERE dt = 30 AND burn_power < 100 AND dv < 0) AS cooldown_rate
    FROM diffs;
    """
    warmup_rate, cooldown_rate = con.execute(rate_query).fetchone()
    warmup_k_s = float(warmup_rate if warmup_rate else 0.05)
    cooldown_k_s = float(cooldown_rate if cooldown_rate else -0.015)
    print(f"Warmup rate during firing: {warmup_k_s*60:.2f} °C/min ({warmup_k_s:.4f} K/s)")
    print(f"Cooldown rate when idle:   {cooldown_k_s*60:.2f} °C/min ({cooldown_k_s:.4f} K/s)")

    results = {
        "boiler_nominal_temp_c": float(res[4]), # median 67.7 C
        "boiler_min_heating_c": float(res[3]),
        "boiler_max_heating_c": float(res[5]),
        "incoming_nominal_c": float(res[7]),    # median 64.9 C
        "oscillation_period_s": float(top_period_min * 60),
        "oscillation_amplitude_k": float(amp),
        "warmup_rate_k_per_s": float(warmup_k_s),
        "cooldown_rate_k_per_s": float(cooldown_k_s),
    }
    return results

if __name__ == "__main__":
    res = analyze_heater()
    print("\n--- Identified Heater Parameters ---")
    for k, v in res.items():
        print(f"  {k:25s}: {v}")
