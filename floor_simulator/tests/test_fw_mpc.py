"""Closed-loop checks of the firmware MPC core (firmware/src/mpc_core.cpp via ctypes)."""
import shutil

import pytest

from floor_simulator.tools.mixer_tuning_sim import run

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="needs a host C++ compiler")


def test_fw_mpc_tracks_with_little_travel():
    r = run(dict(ctrl="fw_mpc", sensor_res=0.0625), hours=4, seed=0, setpoint=35, tout=2)
    assert r["anomalies"] == 0
    assert r["travel_pct_h"] < 60
    assert r["rmse_ok"] < 1.5
    assert r["fwd_max"] < 35 + 3


def test_fw_mpc_radiator_switching_without_anomaly():
    rad = [(3600, 5400, "rad1"), (7200, 9000, "both")]
    r = run(dict(ctrl="fw_mpc", rad_schedule=rad, sensor_res=0.0625), hours=3, seed=1, setpoint=35)
    assert r["anomalies"] == 0 and r["travel_pct_h"] < 80


def test_fw_mpc_valve_fault_homes_and_recovers():
    r = run(dict(ctrl="fw_mpc", pos_fault=(5400, 0.30), sensor_res=0.0625), hours=6, seed=1, setpoint=35)
    kinds = [e[1] for e in r["events"]]
    assert "fault" in kinds and "homed" in kinds
    assert any(k.startswith("home:") for k in kinds)
    assert r["mode"] == "mpc"          # cautious control was stable, MPC retried


def test_fw_mpc_setpoint_drop_is_not_an_anomaly():
    # 35 -> 25 is below the reachable minimum (return water); 28 needs closing
    steps = [(3600, 25.0), (7200, 35.0), (9000, 28.0)]
    r = run(dict(ctrl="fw_mpc", sp_schedule=steps, sensor_res=0.0625), hours=4, seed=0, setpoint=35)
    assert r["anomalies"] == 0 and r["mode"] == "mpc"


def test_fw_mpc_boost_speeds_up_heat_up():
    def reach(params):
        tr = []
        run(dict(ctrl="fw_mpc", sensor_res=0.0625, fw_params=params), hours=1.0, seed=0, setpoint=40,
            tout=-5, trace=tr)
        return next(row[0] for row in tr if row[1] >= 39.0)
    assert reach(dict(boost_scale=0.0)) < 0.5 * reach(dict(boost_scale=1.0))
