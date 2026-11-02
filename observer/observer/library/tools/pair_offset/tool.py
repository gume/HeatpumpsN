import numpy as np
import pandas as pd


def run(a, b, mask=None):
    df = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    if mask is not None:
        m = mask.reindex(df.index).fillna(False).astype(bool)
        df = df[m]
    if df.empty:
        return {"points": 0}
    d = df["a"] - df["b"]
    worst = d.abs().idxmax()
    corr = float(df["a"].corr(df["b"])) if df["a"].std() > 0 and df["b"].std() > 0 else None
    return {"points": int(len(d)), "mean_offset": round(float(d.mean()), 4),
            "median_offset": round(float(d.median()), 4), "spread": round(float(d.std(ddof=0)), 4),
            "p95_abs": round(float(np.percentile(d.abs(), 95)), 4), "max_abs": round(float(d.abs().max()), 4),
            "max_abs_at": worst.isoformat(), "correlation": None if corr is None else round(corr, 4)}
