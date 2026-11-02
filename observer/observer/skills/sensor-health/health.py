"""Helpers for the sensor-health skill (run inside the sandbox)."""
import pandas as pd


def pair_offset(csv_path: str, a: str, b: str) -> dict:
    """Offset a - b between two sensors in a wide CSV from get_series."""
    df = pd.read_csv(csv_path, parse_dates=["time"]).set_index("time")
    both = df[[a, b]].dropna()
    if both.empty:
        return {"points": 0}
    d = both[a] - both[b]
    return {"points": int(len(d)), "mean_offset": round(float(d.mean()), 3),
            "spread": round(float(d.std()), 3), "max_abs": round(float(d.abs().max()), 3)}
