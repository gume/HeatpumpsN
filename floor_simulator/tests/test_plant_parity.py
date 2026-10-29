"""PlantEngine must reproduce step6/plant_v1.plant_run (coupled mode) exactly."""
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from floor_simulator.app.plant import PlantEngine, PlantInputs, PlantParams

REF = Path(__file__).resolve().parents[1] / "log_analysis" / "step6" / "plant_v1.py"


def load_reference():
    # The reference is numba-jitted; a pass-through njit runs it as plain Python.
    if "numba" not in sys.modules:
        stub = types.ModuleType("numba")
        stub.njit = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
        sys.modules["numba"] = stub
    spec = importlib.util.spec_from_file_location("plant_v1_ref", REF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def synthetic_inputs(n, seed):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    burner = ((t // 97) % 3 != 0).astype(float) * 23.0
    tb = np.where(burner > 0, 68.7, 50.0) + rng.normal(0, 1.0, n)
    ww = (((t // 211) % 7) == 3).astype(float)
    bp = np.where((burner > 0) | (ww > 0), 100.0, 0.0)
    p3 = ((t // 150) % 4 != 0)
    p1 = ((t // 60) % 5 == 1)
    p2 = ((t // 80) % 3 == 2)
    pos = np.clip(50 + 30 * np.sin(t / 37.0), 0, 100)
    tout = 2 + 5 * np.sin(2 * np.pi * t / 8640)
    return tb, bp, burner, ww, p1, p2, p3, pos, tout


@pytest.mark.parametrize("act_mode,seed", [(0, 1), (1, 2), (0, 3)])
def test_parity(act_mode, seed):
    ref = load_reference()
    P = PlantParams.load()
    n = 3000
    tb, bp, pm, ww, p1, p2, p3, pos, tout = synthetic_inputs(n, seed)
    init = np.array([45.0, 30.0, 29.0, 21.0, pos[0]])
    z = np.zeros(n)
    out = ref.plant_run(np.array(P.hx), np.array(P.knots), np.array(P.xk), np.array(P.mix),
                        np.array(P.slab), np.array(P.room), np.array([1, act_mode], np.int64),
                        tb, bp, pm, ww, p1, p2, p3, pos, tout,
                        z, z, z, z, z, np.ones(n, bool), np.ones(n, bool), -1, init)
    eng = PlantEngine(P, init=init, act_mode=act_mode)
    keys = ["t_inc_sensor", "t_inc_water", "t_fwd_sensor", "t_ret", "t_slab", "t_room", "pos", "hx_mode"]
    mine = np.empty((n, 8))
    for k in range(n):
        o = eng.step(PlantInputs(tb[k], bp[k], pm[k], bool(ww[k]), bool(p1[k]), bool(p2[k]),
                                 bool(p3[k]), pos[k], tout[k]))
        mine[k] = [o[c] for c in keys]
    assert np.max(np.abs(mine - out)) < 1e-9
