import importlib.util
import shutil
import sys
from pathlib import Path

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
