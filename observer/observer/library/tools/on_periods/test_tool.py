import pandas as pd

from tool import run


def series(pattern, step="1min"):
    return pd.Series(pattern, index=pd.date_range("2026-01-01", periods=len(pattern), freq=step, tz="UTC"))


def test_periods_gaps_and_ongoing():
    s = series([0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 1])
    p = run(s)
    assert list(p["duration_s"]) == [120, 60, 120]
    assert list(p["off_before_s"].fillna(-1)) == [-1, 180, 60]
    assert list(p["ongoing"]) == [False, False, True]


def test_merge_short_gaps_and_drop_short_periods():
    s = series([0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 1])
    p = run(s, min_off_s=90)
    assert len(p) == 2 and p["duration_s"].iloc[1] == 240
    assert len(run(s, min_on_s=100)) == 2
