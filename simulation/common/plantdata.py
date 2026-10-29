"""Shared telemetry loader for the parameter-extraction steps (step2+).

Builds a regular grid (default 10 s) over a time range with causal ASOF
(zero-order hold) joins of every raw series, plus the age of the last raw
sample of each series.  No backward fill is ever used.  The DuckDB file is
opened read-only.

Signals (column names):
  tb    boiler_curflowtemp            [degC]  boiler supply (EMS, 0.1 degC)
  tinc  mixer_incoming_temperature    [degC]  HX secondary outlet at fork
  tfwd  mixer_forward_temperature     [degC]  floor supply after mixer
  tret  mixer_return_temperature      [degC]  floor return manifold
  troom temperature_humidity_sensor_d281_temperature [degC]
  tout  outside_temperature_last_value [degC]
  pos   mixer_state                   [%]     dead-reckoned valve position
  bmi   mixermeter_bmi160_mixer_value [%]     knob accelerometer
  bp    boiler_curburnpow             [%]     burner modulation
  pmod  boiler_heatingpumpmod         [%]     boiler primary pump modulation
  p1w,p2w,p3w heatpumps_pump_N_power  [W]
  ww    boiler_wwcharging             [0/1]
  ha    boiler_heatingactive          [0/1]
  <x>_age  seconds since last raw sample of signal x
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SIM_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("HEATPUMPS_DB_PATH",
                              str(SIM_DIR.parent / "analysis" / "heatpumps.duckdb")))
CACHE_DIR = Path(os.environ.get("HEATPUMPS_CACHE_DIR", str(SIM_DIR / ".cache")))

SEASON_START = "2025-12-01 22:00:00+01:00"
SEASON_END = "2026-05-18 00:00:00+01:00"

SIGNALS = {
    "tb": "boiler_curflowtemp",
    "tinc": "mixer_incoming_temperature",
    "tfwd": "mixer_forward_temperature",
    "tret": "mixer_return_temperature",
    "troom": "temperature_humidity_sensor_d281_temperature",
    "tout": "outside_temperature_last_value",
    "pos": "mixer_state",
    "bmi": "mixermeter_bmi160_mixer_value",
    "bp": "boiler_curburnpow",
    "pmod": "boiler_heatingpumpmod",
    "p1w": "heatpumps_pump_1_power",
    "p2w": "heatpumps_pump_2_power",
    "p3w": "heatpumps_pump_3_power",
    "ww": "boiler_wwcharging",
    "ha": "boiler_heatingactive",
}

PUMP_ON_W = 2.0           # Step 1 binary pump threshold
ANALOG_MAX_AGE_S = 900.0  # Step 1: global silence > 15 min -> outage


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    # timestamps are handled as epoch seconds; no session timezone needed
    return con


def _raw(con, tbl):
    a = con.execute(f"SELECT epoch(t) AS e, v FROM {tbl} ORDER BY t").fetchnumpy()
    return np.asarray(a["e"], dtype=float), np.asarray(a["v"], dtype=float)


def load_grid(t_start: str, t_end: str, step_s: int = 10,
              con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """Causal ZOH grid [t_start, t_end] with per-signal ages.

    For every grid time g the value is the last raw sample with t <= g
    (searchsorted 'right' - 1); the age is g - t_sample.  Grid points before
    the first sample of a series get NaN.  No backward fill.
    """
    own = con is None
    con = con or connect()
    t0 = pd.Timestamp(t_start); t1 = pd.Timestamp(t_end)
    g = np.arange(t0.timestamp(), t1.timestamp() + 0.5 * step_s, float(step_s))
    out = {"t": pd.to_datetime(g, unit="s", utc=True).tz_convert("Europe/Budapest")}
    for k, tbl in SIGNALS.items():
        e, v = _raw(con, tbl)
        idx = np.searchsorted(e, g, side="right") - 1
        ok = idx >= 0
        val = np.full(len(g), np.nan); age = np.full(len(g), np.nan)
        val[ok] = v[idx[ok]]; age[ok] = g[ok] - e[idx[ok]]
        out[k] = val; out[k + "_age"] = age
    if own:
        con.close()
    return pd.DataFrame(out)


def season_grid(step_s: int = 10) -> pd.DataFrame:
    """Whole heating season grid, cached as parquet (cache is regenerable)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / f"season_{step_s}s.parquet"
    if f.exists():
        return pd.read_parquet(f)
    df = load_grid(SEASON_START, SEASON_END, step_s)
    df.to_parquet(f)
    return df


def causal_majority(x: np.ndarray, window: int) -> np.ndarray:
    """Trailing causal majority vote (>= half of the last `window` samples)."""
    s = pd.Series(np.asarray(x, dtype=float))
    rs = s.rolling(window, min_periods=1).sum().values
    rc = s.rolling(window, min_periods=1).count().values
    return 2.0 * rs >= rc


def add_pump_states(df: pd.DataFrame, step_s: int = 10,
                    window_s: float = 310.0) -> pd.DataFrame:
    """Debounced causal pump booleans p1,p2,p3 (5-min trailing majority)."""
    w = max(1, int(round(window_s / step_s)))
    for i in (1, 2, 3):
        raw = (df[f"p{i}w"].fillna(0.0).values >= PUMP_ON_W).astype(float)
        df[f"p{i}"] = causal_majority(raw, w)
    df["rad"] = df["p1"] | df["p2"]
    return df


def split_blocks(split: dict, hours: float = 12.0) -> dict:
    """Blocks of step2/holdout_v7.json as {kind: [(start, end), ...]}.

    A block ends at min(start + hours, next block start); this truncates the
    DST-day block that the Phase 0 selector built as "midnight + 12 h absolute".
    """
    starts = sorted(pd.Timestamp(a) for k in ("FIT", "TUNE", "EXCLUDED") for a in split["split"][k])
    end = {a: min(a + pd.Timedelta(hours=hours), b) for a, b in zip(starts, starts[1:])}
    end[starts[-1]] = starts[-1] + pd.Timedelta(hours=hours)
    return {k: [(pd.Timestamp(a), end[pd.Timestamp(a)]) for a in split["split"][k]]
            for k in ("FIT", "TUNE", "EXCLUDED")}


def blocks_mask(t: pd.Series, blocks: list) -> np.ndarray:
    """Boolean mask of grid times inside any [start, end) block."""
    tv = t.values
    m = np.zeros(len(tv), bool)
    for a, b in blocks:
        i0 = int(np.searchsorted(tv, a.to_datetime64()))
        i1 = int(np.searchsorted(tv, b.to_datetime64()))
        m[i0:i1] = True
    return m
