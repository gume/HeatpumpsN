import numpy as np
import pandas as pd

from tool import run


def test_recovers_gain_tau_and_delay():
    dt, n = 10.0, 600
    t = pd.date_range("2026-01-01", periods=n, freq="10s", tz="UTC")
    rng = np.random.default_rng(1)
    u = np.repeat(rng.uniform(0, 100, n // 40), 40)            # steps every 400 s
    K, tau, L, c = 0.3, 120.0, 40.0, 20.0
    a = np.exp(-dt / tau)
    y = np.full(n, c + K * u[0])
    d = int(L / dt)
    for k in range(n - 1):
        y[k + 1] = a * y[k] + (1 - a) * (K * u[max(0, k - d)] + c)
    y += rng.normal(0, 0.02, n)
    r = run(pd.Series(u, index=t), pd.Series(y, index=t), max_delay_s=200)
    assert abs(r["gain"] - K) < 0.01 and abs(r["tau_s"] - tau) < 10 and r["delay_s"] == L
    assert r["r2"] > 0.99


def test_no_excitation():
    t = pd.date_range("2026-01-01", periods=50, freq="10s", tz="UTC")
    assert "error" in run(pd.Series(1.0, index=t), pd.Series(np.arange(50.0), index=t))
