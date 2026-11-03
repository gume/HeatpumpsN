import importlib.util
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from observer.sim import fwcore, loop
from observer.sim.plant import PlantEngine, PlantInputs, PlantParams

pytestmark = pytest.mark.skipif(not fwcore.available()[0], reason="no MPC core (sources or g++)")
REPO = Path(__file__).resolve().parents[2]


def test_core_defaults_and_validate():
    d = fwcore.default_set("floor")
    assert len(d["knots_pct"]) == 11 and d["delay_s"] > 0
    assert fwcore.validate("floor", {"delay_s": 60}) is None
    bad = dict(d, xk=list(reversed(d["xk"])))
    assert fwcore.validate("floor", bad)                # the firmware's own message
    assert fwcore.full_set("rad1", {"tau_fwd_s": 50, "nonsense": 1})["tau_fwd_s"] == 50


def test_closed_loop_tracks_on_the_identified_house():
    r = loop.run(loop.SyntheticPlant(setpoint=loop.setpoint_steps(35, [(7200, 30)])), seconds=4 * 3600)
    m = r.metrics
    assert m["anomalies"] == 0 and m["fallbacks"] == 0 and m["final_mode"] == "mpc"
    assert m["never_reached"] == 0 and m["setpoints"] == 2
    assert m["rmse_supplied_k"] < 1.5 and m["travel_pct_h"] < 60 and m["overshoot_k"] < 3
    assert r.trace[0][0] == 0 and len(r.trace) == 4 * 360


def test_a_wrong_model_costs_more():
    plant = lambda: loop.SyntheticPlant(setpoint=35, seed=2, rad_schedule=[(3600, 5400, "rad1")])  # noqa: E731
    right = loop.run(plant(), seconds=3 * 3600).metrics
    off = {sc: {"delay_s": 140, "tau_fwd_s": 120} for sc in fwcore.SCENARIOS}
    wrong = loop.run(plant(), off, seconds=3 * 3600).metrics
    assert wrong["starts_h"] > 3 * right["starts_h"] and wrong["travel_pct_h"] > 2 * right["travel_pct_h"]
    assert wrong["overshoot_k"] > right["overshoot_k"]


@pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")
def test_builds_from_a_source_folder(tmp_path, monkeypatch):
    src = tmp_path / "native"
    (src / "src").mkdir(parents=True)
    (src / "include" / "hpn").mkdir(parents=True)
    shutil.copy(REPO / "firmware" / "src" / "mpc_core.cpp", src / "src")
    shutil.copy(REPO / "firmware" / "include" / "hpn" / "mpc_core.h", src / "include" / "hpn")
    monkeypatch.setenv("OBSERVER_MPC_SRC", str(src))
    monkeypatch.setenv("OBSERVER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OBSERVER_MPC_LIB", raising=False)
    lib = fwcore.library_path()
    assert lib.parent == tmp_path / "home" / "cache" and lib.is_file()
    monkeypatch.setenv("OBSERVER_MPC_LIB", str(tmp_path / "missing.so"))
    with pytest.raises(fwcore.CoreUnavailable):
        fwcore.library_path()


FS_PLANT = REPO / "floor_simulator" / "app" / "plant.py"


@pytest.mark.skipif(not FS_PLANT.exists(), reason="floor_simulator not in this checkout")
def test_plant_copy_matches_the_floor_simulator():
    spec = importlib.util.spec_from_file_location("fs_plant", FS_PLANT)
    fs = importlib.util.module_from_spec(spec)
    sys.modules["fs_plant"] = fs
    spec.loader.exec_module(fs)
    a = PlantEngine(PlantParams.load(), act_mode=1)
    b = fs.PlantEngine(fs.PlantParams.load(), act_mode=1)
    assert PlantParams.load().sha256 == fs.PlantParams.load().sha256
    for k in range(2000):
        u = dict(tb=60 + 10 * ((k // 90) % 2), bp=100.0, pmod=40.0 * ((k // 90) % 2), ww=(k // 400) % 5 == 4,
                 p1=(k // 300) % 2 == 1, p2=False, p3=k % 700 < 600, pos=(k * 7) % 100, tout=3.0)
        oa, ob = a.step(PlantInputs(**u)), b.step(fs.PlantInputs(**u))
        assert oa == ob


# --- the closed-loop backtest on recorded situations (sim/replay.py) ---------------------------
from observer import identify  # noqa: E402
from observer.sim import replay  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_identify import CURRENT, manager, synthetic  # noqa: E402

TRUTH = {**CURRENT, "delay_s": 140.0, "tau_fwd_s": 90.0,
         "xk": [0.02, 0.12, 0.28, 0.30, 0.33, 0.36, 0.38, 0.38, 0.38, 0.58, 1.0]}


def recorded(truth, hours=16, seed=3):
    g = synthetic(truth, hours=hours, seed=seed)
    g["sp"] = np.where((np.arange(len(g["t"])) // 1500) % 2, 33.0, 36.0)
    return g


def test_replay_plant_steps_like_the_identification_model():
    g = recorded(TRUTH, hours=3)
    seg = identify.segments(g)[0]
    sl = seg["slice"]
    p = replay.ReplayPlant(g["inc"][sl], g["ret"][sl], g["sp"][sl], 0, TRUTH, float(g["fwd"][sl.start]))
    mine = [p.step(float(g["pos"][sl][k]), k * 10)["t_fwd"] for k in range(sl.stop - sl.start)]
    assert np.max(np.abs(np.array(mine) - identify.simulate(seg, TRUTH, g))) < 1e-9


def test_backtest_compares_both_sets_on_both_mixers():
    g = recorded(TRUTH)
    cur = fwcore.full_set("floor", CURRENT)
    prop = dict(cur, **{k: TRUTH[k] for k in identify.MODEL_FIELDS})
    r = replay.backtest(g, "floor", cur, prop, TRUTH, max_count=4)
    assert r["status"] == "done" and r["situations"] == 4 and len(r["rows"]) == 4
    on_truth = r["plants"]["identified"]
    assert on_truth["proposed"]["rmse_k"] <= on_truth["current"]["rmse_k"] + 0.02     # the matched set is not worse
    assert set(on_truth["current"]) >= {"rmse_k", "overshoot_k", "travel_pct_h", "starts_h", "fallbacks"}
    ex = r["example"]
    assert len(ex["current"]) == len(ex["proposed"]) and len(ex["measured"]) >= len(ex["current"])
    assert r["verdict"] in ("recommended", "neutral", "not recommended")
    assert replay.backtest({**g, "sp": np.full(len(g["t"]), np.nan)}, "floor", cur, prop, TRUTH)["situations"] == 0


def test_verdict_rules():
    base = dict(rmse_k=1.0, travel_pct_h=30, starts_h=5, fallbacks=0, anomalies=0)
    assert replay._verdict(base, dict(base, rmse_k=0.8)) == "better"
    assert replay._verdict(base, dict(base, rmse_k=0.98)) == "similar"
    assert replay._verdict(base, dict(base, rmse_k=0.8, travel_pct_h=60)) == "similar"   # bought with movement
    assert replay._verdict(base, dict(base, rmse_k=1.2)) == "worse"
    assert replay._verdict(base, dict(base, rmse_k=0.5, fallbacks=1)) == "worse (more fallbacks)"


def test_a_new_proposal_is_backtested(tmp_path):
    m, _ = manager(tmp_path)
    g = recorded(TRUTH)
    result = dict(identify.identify(g, m.current_sets()), inputs="exact (slow log)")
    assert result["scenarios"]["floor"]["status"] == "proposal"
    m._proposals_from(result, g, "slow")
    p = m.proposals[-1]
    assert p["source"] == "slow" and p["backtest"]["status"] == "done"
    assert p["backtest"]["assumed"]["min_open_pct"] == 40
    m.backtest_cfg["min_open_pct"] = 20                        # a re-run uses the data again
    m.service.agents = type("A", (), {"data": object()})()
    import observer.parameters as P
    orig = P.identify.fetch
    P.identify.fetch = lambda data, window, source: g
    try:
        bt = m.backtest_now(p["id"])
    finally:
        P.identify.fetch = orig
    assert bt["assumed"]["min_open_pct"] == 20 and m.proposals[-1]["backtest"] is bt
