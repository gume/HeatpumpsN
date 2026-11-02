import numpy as np
import pandas as pd


def run(frame, step="10s", max_hold_s=180):
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame needs a DatetimeIndex")
    frame = frame.sort_index()
    grid = pd.date_range(frame.index[0].floor(step), frame.index[-1].ceil(step), freq=step)
    out = {}
    for col in frame.columns:
        s = frame[col].dropna()
        if s.empty:
            out[col] = pd.Series(np.nan, index=grid)
            continue
        hold = max_hold_s.get(col, 180) if isinstance(max_hold_s, dict) else max_hold_s
        pos = s.index.searchsorted(grid, side="right") - 1          # last sample at or before each grid time
        valid = pos >= 0
        vals = np.full(len(grid), np.nan)
        vals[valid] = s.values[pos[valid]]
        age = np.full(len(grid), np.inf)
        age[valid] = (grid[valid] - s.index[pos[valid]]).total_seconds()
        vals[age > hold] = np.nan
        out[col] = pd.Series(vals, index=grid)
    df = pd.DataFrame(out)
    df.index.name = frame.index.name or "time"
    return df
