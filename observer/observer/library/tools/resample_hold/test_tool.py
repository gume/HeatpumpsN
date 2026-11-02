import numpy as np
import pandas as pd

from tool import run


def test_holds_within_horizon_and_not_across_outages():
    t = pd.to_datetime(["2026-01-01 00:00:00", "2026-01-01 00:00:30", "2026-01-01 00:20:00"], utc=True)
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0]}, index=t)
    g = run(df, step="10s", max_hold_s=60)
    assert g.loc["2026-01-01 00:00:20+00:00", "a"] == 1.0
    assert g.loc["2026-01-01 00:01:30+00:00", "a"] == 2.0          # 60 s old: still held
    assert np.isnan(g.loc["2026-01-01 00:01:40+00:00", "a"])       # 70 s old: outage
    assert g.loc["2026-01-01 00:20:00+00:00", "a"] == 3.0


def test_per_column_hold():
    t = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    df = pd.DataFrame({"fast": [1.0, 2.0], "slow": [5.0, 6.0]}, index=t)
    g = run(df, step="1min", max_hold_s={"fast": 60, "slow": 600})
    assert np.isnan(g.iloc[3]["fast"]) and g.iloc[3]["slow"] == 5.0
