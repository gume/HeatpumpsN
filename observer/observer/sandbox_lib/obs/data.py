"""Read-only data of the house (through the observer; times in UTC).

Times: "now", a relative duration ("-6h", "-30d") or an ISO time.
every: "raw" for every sample, or a window like "30s", "1m", "15m", "1h".
"""
from ._client import call


def find(text="", location="", circuit="", role="", quantity="", source="", stored_only=True, limit=200):
    """Signals from the registry: [{id, name, unit, quantity, location, circuit, role, description, ...}]."""
    return call("find", text=text, location=location, circuit=circuit, role=role, quantity=quantity,
                source=source, stored_only=stored_only, limit=limit)["signals"]


def coverage(signals):
    """{id: {first, last, points}}: how much history each signal has."""
    return call("coverage", signals=list(signals))


def series(signals, start="-24h", stop="now", every="1m", agg="mean", wide=True):
    """Recorded data as a pandas DataFrame indexed by UTC time.

    wide=True: one column per signal (outer join; windows align, raw samples do not).
    wide=False: long format with columns time, signal, value."""
    import pandas as pd

    raw = call("series", signals=list(signals), start=start, stop=stop, every=every, agg=agg)
    frames = {}
    for sig, s in raw.items():
        idx = pd.to_datetime(s["t"], unit="ms", utc=True)
        frames[sig] = pd.Series(s["v"], index=idx, name=sig)
    if not wide:
        rows = [(t, sig, v) for sig, ser in frames.items() for t, v in ser.items()]
        return pd.DataFrame(rows, columns=["time", "signal", "value"]).sort_values("time", ignore_index=True)
    if not frames:
        return pd.DataFrame(columns=list(signals))
    df = pd.concat(frames.values(), axis=1).sort_index()
    df.index.name = "time"
    return df.reindex(columns=[s for s in signals if s in df.columns])


def events(start="-24h", stop="now", min_severity=0, text="", limit=500):
    """The heating controller's events: [{time, code, severity, detail}]."""
    return call("events", start=start, stop=stop, min_severity=min_severity, text=text, limit=limit)["events"]


def house():
    """The house description, the owner's description (current and previous) and the machine model."""
    return call("house")
