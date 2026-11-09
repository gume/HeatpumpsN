import hashlib
import hmac
import json

import numpy as np
import pytest

from observer import identify as I
from observer.parameters import ParameterManager, sign_envelope

KNOTS = [0, 25, 50, 52.5, 55, 57.5, 60, 65, 70, 80, 90]
CURRENT = {"knots_pct": KNOTS, "xk": [0.02, 0.088, 0.196, 0.214, 0.244, 0.275, 0.289, 0.289, 0.289, 0.511, 1.0],
           "delay_s": 40.0, "tau_fwd_s": 34.64, "tau_inc_s": 75.27}


def synthetic(true: dict, hours: float = 12, seed: int = 0, noise: float = 0.05) -> dict:
    """A plant that is exactly the firmware model with `true` parameters, in 2 h floor-pump runs."""
    rng = np.random.default_rng(seed)
    n = int(hours * 3600 / I.DT)
    t = np.arange(n) * I.DT + 1.7e9
    on = ((np.arange(n) * I.DT) % 9000) < 7200                   # 2 h on, 30 min off
    inc = 55 + 6 * np.sin(np.arange(n) / 90) + rng.normal(0, 0.2, n).cumsum() * 0.02
    ret = 30 + 2 * np.sin(np.arange(n) / 400)
    pos = np.repeat(rng.uniform(20, 62, n // 60 + 1), 60)[:n]   # moves every 10 min
    g = {"t": t, "inc": inc, "ret": ret, "pos": pos, "floor": on.astype(float), "r1": np.zeros(n),
         "r2": np.zeros(n), "dhw": np.zeros(n), "homing": np.zeros(n), "fwd": np.zeros(n), "model": np.full(n, np.nan)}
    # the plant output: simulate each run with the true model
    g["fwd"] = ret.copy()
    for s in I.segments({**g, "fwd": ret.copy()}, skip_start_s=0, min_contrast_k=0):
        g["fwd"][s["slice"]] = I.simulate(s, true, {**g, "fwd": g["fwd"]})
    g["fwd"] = g["fwd"] + rng.normal(0, noise, n)
    return g


def test_simulate_matches_the_firmware_steps():
    g = synthetic(CURRENT, hours=3)
    s = I.segments(g)[0]
    sl = s["slice"]
    # a step-by-step transcription of Controller::model_step (firmware/src/mpc_core.cpp)
    inc, ret, pos, fwd = g["inc"][sl], g["ret"][sl], g["pos"][sl], g["fwd"][sl]
    cells = int(round(CURRENT["delay_s"] / 10))
    ring, rp, s_f, slope, prev = [fwd[0]] * cells, 0, fwd[0], 0.0, inc[0]
    out = []
    for k in range(len(inc)):
        slope += 0.3 * ((inc[k] - prev) / 10 - slope)
        prev = inc[k]
        tw = inc[k] + CURRENT["tau_inc_s"] * slope
        tmix = ret[k] + np.interp(pos[k], KNOTS, CURRENT["xk"]) * (tw - ret[k])
        delayed = ring[rp]
        ring[rp] = tmix
        rp = (rp + 1) % cells
        s_f += 10 / (CURRENT["tau_fwd_s"] + 10) * (delayed - s_f)
        out.append(s_f)
    assert np.max(np.abs(np.array(out) - I.simulate(s, CURRENT, g))) < 1e-9


def test_identification_recovers_a_changed_plant_and_proposes():
    true = {**CURRENT, "delay_s": 70.0, "tau_fwd_s": 50.0,
            "xk": [0.02, 0.10, 0.25, 0.27, 0.30, 0.33, 0.35, 0.35, 0.35, 0.55, 1.0]}
    g = synthetic(true)
    r = I.identify(g, {"floor": CURRENT})["scenarios"]["floor"]
    assert r["status"] == "proposal", r
    new = r["identified"]
    assert new["delay_s"] == 70.0 and abs(new["tau_fwd_s"] - 50) < 12
    assert r["rmse"]["holdout_identified"] < 0.3 < r["rmse"]["holdout_current"]
    # knots the valve never visited keep the current law where monotonicity allows (65/70 % must
    # rise to the new value at 60 %; 80/90 % keep theirs)
    assert new["xk"][9:] == pytest.approx(CURRENT["xk"][9:], abs=0.02)
    assert new["xk"][2] == pytest.approx(0.25, abs=0.02) and new["xk"][4] == pytest.approx(0.30, abs=0.02)
    assert all(b >= a for a, b in zip(new["xk"], new["xk"][1:]))       # monotone


def test_no_proposal_when_the_current_model_fits():
    g = synthetic(CURRENT, seed=3)
    r = I.identify(g, {"floor": CURRENT})["scenarios"]["floor"]
    assert r["status"] == "current parameters are as good"
    assert I.identify({**g, "floor": np.zeros(len(g["t"]))}, {"floor": CURRENT})["scenarios"]["floor"]["status"] \
        == "no usable data"


def test_envelope_matches_the_firmware_tool():
    env = json.loads(sign_envelope({"set": "floor", "delay_s": 45}, "s3cret"))
    msg = json.loads(env["msg"])
    assert msg["set"] == "floor" and msg["delay_s"] == 45 and msg["seq"] > 1.7e12
    assert env["sig"] == hmac.new(b"s3cret", env["msg"].encode(), hashlib.sha256).hexdigest()


class Mqtt:
    connected = True

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload))


def manager(tmp_path, push=True):
    svc = type("S", (), {"mqtt": Mqtt(), "_supervisor": None})()
    cfg = {"paths": {"catalog": str(tmp_path / "catalog.json")}, "parameters": {"push_enabled": push}}
    m = ParameterManager(svc, cfg)
    m.on_raw("heatpumpsn/controller/state/mpc/params/floor",
             json.dumps({"set": "floor", "revision": 0, **CURRENT, "horizon_s": 900}).encode(), True)
    return m, svc


def test_approve_send_ack_and_rollback(tmp_path, monkeypatch):
    m, svc = manager(tmp_path)
    assert m.current_sets()["floor"]["delay_s"] == 40.0
    new = {**CURRENT, "delay_s": 70.0}
    m.proposals.append({"id": "p1", "created": "x", "scenario": "floor", "status": "proposed",
                        "current": CURRENT, "identified": new})
    monkeypatch.setenv("OBSERVER_CONTROLLER_SECRET", "s3cret")
    m.approve("p1")
    topic, payload = svc.mqtt.sent[-1]
    assert topic == "heatpumpsn/controller/cmd/mpc/params"
    cmd = json.loads(json.loads(payload)["msg"])
    assert cmd["set"] == "floor" and cmd["delay_s"] == 70.0 and cmd["persist"] is True
    assert "horizon_s" not in cmd                                       # only the model fields
    with pytest.raises(ValueError):
        m.approve("p1")
    m.on_raw("heatpumpsn/controller/state/mpc/ack", b'{"ok":true,"source":"mqtt","what":"params","error":""}', False)
    assert m._proposal("p1")["status"] == "accepted"
    rb = m.rollback("p1")
    cmd = json.loads(json.loads(svc.mqtt.sent[-1][1])["msg"])
    assert cmd["delay_s"] == 40.0 and rb["rollback_of"] == "p1" and m._proposal("p1")["status"] == "rolled back"
    m.on_raw("heatpumpsn/controller/state/mpc/ack",
             b'{"ok":false,"source":"mqtt","what":"params","error":"delay_s"}', False)
    assert m._proposal(rb["id"])["status"] == "rejected by the controller"


def test_sending_needs_push_enabled(tmp_path):
    m, svc = manager(tmp_path, push=False)
    m.proposals.append({"id": "p1", "created": "x", "scenario": "floor", "status": "proposed",
                        "current": CURRENT, "identified": CURRENT})
    with pytest.raises(RuntimeError, match="disabled"):
        m.approve("p1")
    assert svc.mqtt.sent == [] and m._proposal("p1")["status"] == "proposed"


class _Series:
    def __init__(self, times, values):
        self.times, self.values = times, values


class _DataSource:
    """Only the signals it is given; everything else has no rows, like a partial slow log."""

    def __init__(self, out: dict):
        self.out = out

    def series(self, ids, start, stop="now", every="raw", agg="mean", limit=5_000_000):
        return {k: v for k, v in self.out.items() if k in ids}


def test_fetch_tolerates_a_missing_slow_signal():
    """A slow-log signal with no rows must read as NaN, not crash with KeyError: 'pos'."""
    import datetime as dt

    base = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
    times = [base + dt.timedelta(seconds=I.DT * i) for i in range(30)]
    data = _DataSource({
        I.SIGNALS["inc"]: _Series(times, [50.0] * 30),
        I.SIGNALS["ret"]: _Series(times, [30.0] * 30),
        I.SIGNALS["fwd"]: _Series(times, [32.0] * 30),
        # I.SIGNALS["pos"] deliberately absent
    })
    g = I.fetch(data, "-1h", "now", source="slow")
    assert np.isnan(g["pos"]).all()
    assert I.segments(g) == []


def test_fetch_without_any_data_still_raises():
    with pytest.raises(ValueError, match="no data"):
        I.fetch(_DataSource({}), "-1h", "now", source="slow")
