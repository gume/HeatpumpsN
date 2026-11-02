import pandas as pd

from tool import run


def test_flicker_is_removed_and_real_switch_kept():
    t = pd.date_range("2026-01-01", periods=120, freq="2s", tz="UTC")
    power = pd.Series([0.0] * 60 + [5.0] * 60, index=t)
    power.iloc[[10, 11, 30]] = 5.0          # 2-4 s flicker while off
    power.iloc[[80, 95]] = 0.0              # dropouts while on
    st = run(power, threshold=2.0, window="1min")
    assert st.iloc[:60].sum() == 0
    assert st.iloc[-1] == 1 and st.iloc[75:].min() == 1
    assert ((st.diff() == 1).sum()) == 1    # one switch-on
