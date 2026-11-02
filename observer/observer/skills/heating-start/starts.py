"""Helpers for the heating-start skill (run inside the sandbox)."""
import pandas as pd


def load(csv_path: str) -> pd.DataFrame:
    return pd.read_csv(csv_path, parse_dates=["time"]).set_index("time").sort_index()


def _indexed(df: pd.DataFrame) -> pd.DataFrame:
    """Accept a frame straight from read_csv as well as one indexed by time."""
    if not isinstance(df.index, pd.DatetimeIndex) and "time" in df.columns:
        df = df.assign(time=pd.to_datetime(df["time"])).set_index("time").sort_index()
    return df


def find_starts(df: pd.DataFrame, pump: str, min_off_min: int = 10) -> list:
    """Times where the pump goes from off to on after at least min_off_min minutes off."""
    df = _indexed(df)
    on = (df[pump].ffill().fillna(0) >= 0.5).astype(int)
    starts, last_off = [], None
    for t, v in on.items():
        if v == 0 and last_off is None:
            last_off = t
        elif v == 1 and last_off is not None:
            if (t - last_off).total_seconds() >= min_off_min * 60:
                starts.append(t)
            last_off = None
    return starts


def review_start(df: pd.DataFrame, start, forward: str, setpoint, mixer: str = None,
                 ret: str = None, horizon_min: int = 90) -> dict:
    """Figures of one start; setpoint is a column name or a number.

    Rarely published signals (the setpoint) are carried forward from before
    the start, so the value in force at the start is used."""
    df = _indexed(df)
    start = pd.Timestamp(start)
    end = start + pd.Timedelta(minutes=horizon_min)
    w = df.loc[start:end]
    sp = df[setpoint].ffill().loc[start:end] if isinstance(setpoint, str) else pd.Series(setpoint, index=w.index)
    fwd = w[forward]
    near = fwd[(sp - fwd) <= 2.0]
    out = {"start": str(start), "forward_at_start": _r(fwd.iloc[0]) if len(fwd) else None,
           "setpoint": _r(sp.iloc[0]) if len(sp) else None,
           "minutes_to_within_2K": round((near.index[0] - start).total_seconds() / 60, 1) if len(near) else None,
           "overshoot_K": _r((fwd - sp).max()) if len(fwd) else None}
    for minutes in (5, 15, 30):
        if mixer:
            seg = df[mixer].ffill().loc[start:start + pd.Timedelta(minutes=minutes)].dropna()
            out[f"mixer_after_{minutes}min"] = _r(seg.iloc[-1]) if len(seg) else None
    if ret:
        out["return_after_30min"] = _r(w[ret].loc[:start + pd.Timedelta(minutes=30)].dropna().iloc[-1]) \
            if w[ret].notna().any() else None
    return out


def _r(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None
