import numpy as np
import pandas as pd


def _at(series, t):
    s = series.loc[:t].dropna()
    return float(s.iloc[-1]) if len(s) else np.nan


def run(value, setpoint, starts, band=2.0, horizon_min=90, companions=None, sample_min=(5, 15, 30)):
    value = value.sort_index()
    rows = []
    for start in starts:
        start = pd.Timestamp(start)
        end = start + pd.Timedelta(minutes=horizon_min)
        w = value.loc[start:end].dropna()
        if isinstance(setpoint, pd.Series):
            sp = setpoint.sort_index().ffill().reindex(w.index, method="ffill")
            sp0 = _at(setpoint.sort_index(), start)
            sp = sp.fillna(sp0)
        else:
            sp = pd.Series(float(setpoint), index=w.index)
            sp0 = float(setpoint)
        row = {"start": start, "value_at_start": _at(value, start), "setpoint": sp0}
        near = w[(sp - w) <= band]
        row["minutes_to_band"] = round((near.index[0] - start).total_seconds() / 60, 1) if len(near) else np.nan
        row["overshoot"] = round(float((w - sp).max()), 2) if len(w) else np.nan
        for label, comp in (companions or {}).items():
            comp = comp.sort_index()
            for m in sample_min:
                row[f"{label}_{m}min"] = _at(comp, start + pd.Timedelta(minutes=m))
        rows.append(row)
    return pd.DataFrame(rows)
