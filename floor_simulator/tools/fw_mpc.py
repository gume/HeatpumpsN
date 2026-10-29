"""ctypes binding to the firmware MPC core, compiled for the host on demand."""
from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
FW = HERE.parents[1] / "firmware"
SRC = [FW / "src" / "mpc_core.cpp", HERE / "mpc_host_shim.cpp"]
LIB = HERE.parent / ".cache" / "libmpc_host.so"

CMD_KEEP, CMD_TARGET, CMD_HOME = 0, 1, 2
MODE_LEGACY, MODE_MPC, MODE_HOMING, MODE_CAUTIOUS = 0, 1, 2, 3
MODES = {0: "legacy", 1: "mpc", 2: "homing", 3: "cautious"}
ANOMALIES = {0: "none", 1: "model_bias", 2: "prediction_miss", 3: "overshoot", 4: "numeric", 5: "manual"}


def build() -> Path:
    deps = SRC + [FW / "include" / "hpn" / "mpc_core.h"]
    if not LIB.exists() or LIB.stat().st_mtime < max(p.stat().st_mtime for p in deps):
        LIB.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-fPIC", "-shared",
                               f"-I{FW / 'include'}", *map(str, SRC), "-o", str(LIB)])
    return LIB


_lib = None


def lib():
    global _lib
    if _lib is None:
        L = ctypes.CDLL(str(build()))
        f, i, p = ctypes.c_float, ctypes.c_int, ctypes.c_void_p
        L.mpc_new.restype = p
        L.mpc_free.argtypes = [p]
        L.mpc_set_default.argtypes = [p, i]
        L.mpc_set_params.argtypes = [p, i, ctypes.POINTER(f), ctypes.POINTER(f), ctypes.POINTER(f)]
        L.mpc_request_mode.argtypes = [p, i, f]
        L.mpc_on_homed.argtypes = [p, f]
        L.mpc_reset_model.argtypes = [p]
        L.mpc_tick.argtypes = [p, f, f, f, f, f, f, f, f, f, f, f, i, ctypes.POINTER(f)]
        L.mpc_tick.restype = i
        L.mpc_status.argtypes = [p, ctypes.POINTER(f), ctypes.POINTER(i)]
        _lib = L
    return _lib


class FwMpc:
    def __init__(self):
        self.L = lib()
        self.h = self.L.mpc_new()
        self._t = ctypes.c_float()

    def __del__(self):
        try:
            self.L.mpc_free(self.h)
        except Exception:  # noqa: BLE001
            pass

    def set_params(self, scenario, knots=None, xk=None, scalars=None):
        arr = lambda v: (ctypes.c_float * len(v))(*v) if v is not None else None  # noqa: E731
        return self.L.mpc_set_params(self.h, scenario, arr(knots), arr(xk), arr(scalars))

    def set_default(self, scenario):
        self.L.mpc_set_default(self.h, scenario)

    def request_mode(self, mode, now):
        self.L.mpc_request_mode(self.h, mode, now)

    def on_homed(self, now):
        self.L.mpc_on_homed(self.h, now)

    def tick(self, now, pos, target, t_inc, t_ret, t_fwd, setpoint, pmin, pmax, travel_s, overshoot_delta,
             scenario):
        k = self.L.mpc_tick(self.h, now, pos, target, t_inc, t_ret, t_fwd, setpoint, pmin, pmax, travel_s,
                            overshoot_delta, scenario, ctypes.byref(self._t))
        return k, self._t.value

    def status(self):
        f = (ctypes.c_float * 4)()
        i = (ctypes.c_int * 6)()
        self.L.mpc_status(self.h, f, i)
        return dict(mode=MODES[i[0]], anomaly=ANOMALIES.get(i[1], "?"), retries=i[2], plans=i[3],
                    triggered=i[4], anomalies=i[5], bias=f[0], tinc_water=f[1], model_fwd=f[2], cost=f[3])
