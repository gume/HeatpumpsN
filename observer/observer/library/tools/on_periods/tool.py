import numpy as np
import pandas as pd


def run(series, threshold=0.5, min_off_s=0, min_on_s=0):
    s = series.dropna().sort_index()
    on = (s.astype(float) >= threshold).to_numpy()
    t = s.index
    periods, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = t[i]
        elif not v and start is not None:
            periods.append([start, t[i], False])
            start = None
    if start is not None:
        periods.append([start, t[-1], True])
    merged = []
    for p in periods:
        if merged and (p[0] - merged[-1][1]).total_seconds() < min_off_s:
            merged[-1][1], merged[-1][2] = p[1], p[2]
        else:
            merged.append(p)
    rows = []
    for p in merged:
        dur = (p[1] - p[0]).total_seconds()
        if dur >= min_on_s:
            rows.append({"start": p[0], "end": p[1], "duration_s": dur, "ongoing": p[2]})
    df = pd.DataFrame(rows, columns=["start", "end", "duration_s", "ongoing"])
    df.insert(3, "off_before_s", np.nan)
    if len(df) > 1:
        df.loc[1:, "off_before_s"] = (df["start"].iloc[1:].values - df["end"].iloc[:-1].values) / np.timedelta64(1, "s")
    return df
