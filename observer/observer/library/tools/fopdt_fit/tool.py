import numpy as np
import pandas as pd


def _simulate(a, b, c, u, y0):
    y = np.empty(len(u))
    y[0] = y0
    for k in range(len(u) - 1):
        y[k + 1] = a * y[k] + b * u[k] + c
    return y


def run(u, y, max_delay_s=600, delay_step_s=0):
    df = pd.concat([u.rename("u"), y.rename("y")], axis=1).dropna()
    if len(df) < 20:
        return {"error": "fewer than 20 common points"}
    dt = float(np.median(np.diff(df.index.values).astype("timedelta64[ms]").astype(float))) / 1000
    if dt <= 0:
        return {"error": "irregular or duplicate time stamps"}
    uu, yy = df["u"].to_numpy(float), df["y"].to_numpy(float)
    if np.std(uu) == 0:
        return {"error": "the input does not change: no excitation"}
    step = max(1, int(round((delay_step_s or dt) / dt)))
    best = None
    for d in range(0, int(max_delay_s / dt) + 1, step):
        if len(uu) - d < 20:
            break
        ud, yd = uu[: len(uu) - d], yy[d:]
        X = np.column_stack([yd[:-1], ud[:-1], np.ones(len(yd) - 1)])
        coef, *_ = np.linalg.lstsq(X, yd[1:], rcond=None)
        a, b, c = coef
        if not 0 < a < 1:
            continue
        sim = _simulate(a, b, c, ud, yd[0])
        rmse = float(np.sqrt(np.mean((sim - yd) ** 2)))
        if best is None or rmse < best[0]:
            ss = float(np.sum((yd - yd.mean()) ** 2))
            r2 = 1 - float(np.sum((sim - yd) ** 2)) / ss if ss > 0 else None
            best = (rmse, d, a, b, c, r2, len(yd))
    if best is None:
        return {"error": "no stable first-order fit (the data may not be first-order or lacks excitation)"}
    rmse, d, a, b, c, r2, n = best
    return {"gain": round(b / (1 - a), 4), "tau_s": round(-dt / np.log(a), 1), "delay_s": round(d * dt, 1),
            "offset": round(c / (1 - a), 4), "rmse": round(rmse, 4), "r2": None if r2 is None else round(r2, 4),
            "points": int(n), "dt_s": dt}
