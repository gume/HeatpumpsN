import numpy as np
import pandas as pd

from tool import run


def test_offset_spread_and_mask():
    t = pd.date_range("2026-01-01", periods=100, freq="1min", tz="UTC")
    b = pd.Series(np.linspace(20, 40, 100), index=t)
    a = b + 1.5
    a.iloc[50] += 10
    r = run(a, b)
    assert r["points"] == 100 and abs(r["median_offset"] - 1.5) < 1e-9
    assert r["max_abs"] == 11.5 and r["max_abs_at"].startswith("2026-01-01T00:50")
    m = pd.Series(True, index=t)
    m.iloc[50] = False
    assert run(a, b, m)["max_abs"] == 1.5
    assert run(a.iloc[:0], b)["points"] == 0
