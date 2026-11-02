import numpy as np
import pandas as pd

from tool import run


def test_time_to_band_overshoot_and_companions():
    t = pd.date_range("2026-01-01 10:00", periods=121, freq="1min", tz="UTC")
    value = pd.Series(np.r_[np.full(10, 20.0), np.linspace(20, 37, 30), np.full(81, 36.0)], index=t)
    setpoint = pd.Series([35.0], index=t[:1])                 # published once, long before
    valve = pd.Series(np.linspace(0, 100, 121), index=t)
    r = run(value, setpoint, [t[10]], companions={"valve": valve}).iloc[0]
    assert r["setpoint"] == 35.0 and r["value_at_start"] == 20.0
    assert 20 <= r["minutes_to_band"] <= 27
    assert r["overshoot"] == 2.0
    assert abs(r["valve_15min"] - valve.iloc[25]) < 1e-9


def test_constant_setpoint_never_reached():
    t = pd.date_range("2026-01-01", periods=30, freq="1min", tz="UTC")
    r = run(pd.Series(20.0, index=t), 35, [t[0]]).iloc[0]
    assert np.isnan(r["minutes_to_band"])
