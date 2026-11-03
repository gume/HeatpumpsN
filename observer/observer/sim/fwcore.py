"""The firmware's MPC core (firmware/src/mpc_core.cpp), compiled for this host and bound with ctypes.

The same C++ the controller runs, so a simulation decides exactly as the
controller would. Where the library comes from, in order:

1. ``OBSERVER_MPC_LIB``: a prebuilt library (the image compiles it while it
   is built, see the Dockerfile);
2. compiled on first use with the host's ``g++`` from the core's sources:
   ``OBSERVER_MPC_SRC``, the repository's ``firmware/`` (development) or the
   bundle's ``native/`` (scripts/native.sh copies them from firmware/).
   The result is cached by the sources' hash, so a firmware change rebuilds.

Each source folder has ``src/mpc_core.cpp`` and ``include/hpn/mpc_core.h``.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "mpc_shim.cpp"

SCENARIOS = ("floor", "rad1", "rad2", "both")
CMD_KEEP, CMD_TARGET, CMD_HOME = 0, 1, 2
MODE_LEGACY, MODE_MPC, MODE_HOMING, MODE_CAUTIOUS = 0, 1, 2, 3
MODES = {0: "legacy", 1: "mpc", 2: "homing", 3: "cautious"}
ANOMALIES = {0: "none", 1: "model_bias", 2: "prediction_miss", 3: "overshoot", 4: "numeric", 5: "manual"}
SCALARS = ("delay_s", "tau_fwd_s", "tau_inc_s", "horizon_s", "replan_s", "trigger_k", "lambda_travel",
           "start_cost", "margin_k", "bias_alpha", "supply_margin_k", "boost_s", "boost_scale", "boost_error_k")


class CoreUnavailable(RuntimeError):
    """No library and no way to build one (no sources or no compiler)."""


def source_dirs() -> list[Path]:
    out = []
    if os.environ.get("OBSERVER_MPC_SRC"):
        out.append(Path(os.environ["OBSERVER_MPC_SRC"]))
    out.append(HERE.parents[2] / "firmware")            # the repository (before a copy in native/)
    out.append(HERE.parents[1] / "native")              # the release bundle
    return out


def find_sources() -> Path | None:
    for d in source_dirs():
        if (d / "src" / "mpc_core.cpp").is_file() and (d / "include" / "hpn" / "mpc_core.h").is_file():
            return d
    return None


def sources_hash(src: Path) -> str:
    h = hashlib.sha256()
    for p in (src / "src" / "mpc_core.cpp", src / "include" / "hpn" / "mpc_core.h", SHIM):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def compile_lib(src: Path, out: Path) -> Path:
    """g++ -shared: the core plus the shim. Raises CoreUnavailable without a compiler."""
    cxx = os.environ.get("CXX") or shutil.which("g++") or shutil.which("c++")
    if not cxx:
        raise CoreUnavailable("no C++ compiler (g++) to build the firmware MPC core")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    r = subprocess.run([cxx, "-std=c++17", "-O2", "-fPIC", "-shared", f"-I{src / 'include'}",
                        str(src / "src" / "mpc_core.cpp"), str(SHIM), "-o", str(tmp)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        tmp.unlink(missing_ok=True)
        raise CoreUnavailable(f"building the MPC core failed: {r.stderr.strip()[-800:]}")
    os.replace(tmp, out)
    return out


def _cache_dir() -> Path:
    home = os.environ.get("OBSERVER_HOME") or os.path.expanduser("~/.local/share/heatpumpsn-observer")
    return Path(home) / "cache"


def library_path() -> Path:
    pre = os.environ.get("OBSERVER_MPC_LIB")
    if pre:
        if not Path(pre).is_file():
            raise CoreUnavailable(f"OBSERVER_MPC_LIB={pre} does not exist")
        return Path(pre)
    src = find_sources()
    if src is None:
        raise CoreUnavailable("the firmware MPC sources are not here (native/ or firmware/)")
    lib = _cache_dir() / f"libmpc_core-{sources_hash(src)}.so"
    return lib if lib.is_file() else compile_lib(src, lib)


_lib = None
_lock = threading.Lock()


def lib():
    global _lib
    with _lock:
        if _lib is None:
            L = ctypes.CDLL(str(library_path()))
            f, i, p, d = ctypes.c_float, ctypes.c_int, ctypes.c_void_p, ctypes.c_double
            fp, ip = ctypes.POINTER(f), ctypes.POINTER(i)
            L.mpc_new.restype = p
            L.mpc_free.argtypes = [p]
            L.mpc_default.argtypes = [i, fp, fp, fp]
            L.mpc_set.argtypes = [p, i, fp, fp, fp, ctypes.c_char_p, i]
            L.mpc_set.restype = i
            L.mpc_request_mode.argtypes = [p, i, d]
            L.mpc_on_homed.argtypes = [p, d]
            L.mpc_reset_model.argtypes = [p]
            L.mpc_tick.argtypes = [p, d, f, f, f, f, f, i, i, i, f, f, f, f, f, i, fp]
            L.mpc_tick.restype = i
            L.mpc_status.argtypes = [p, fp, ip]
            if L.mpc_knots() != 11 or L.mpc_scalars() != len(SCALARS):
                raise CoreUnavailable("the MPC core's parameter layout changed: update observer/sim")
            _lib = L
    return _lib


def available() -> tuple[bool, str]:
    """(usable, where it comes from or why not)."""
    try:
        lib()
        return True, os.environ.get("OBSERVER_MPC_LIB") or str(find_sources())
    except (CoreUnavailable, OSError) as e:
        return False, str(e)


def scenario_index(scenario) -> int:
    return scenario if isinstance(scenario, int) else SCENARIOS.index(scenario)


def default_set(scenario) -> dict:
    """The firmware's built-in set of a scenario, in the JSON form of state/mpc/params/<set>."""
    k, x, s = (ctypes.c_float * 11)(), (ctypes.c_float * 11)(), (ctypes.c_float * len(SCALARS))()
    lib().mpc_default(scenario_index(scenario), k, x, s)
    out = {"knots_pct": [round(v, 4) for v in k], "xk": [round(v, 5) for v in x]}
    out.update({n: round(v, 5) for n, v in zip(SCALARS, s)})
    return out


def full_set(scenario, params: dict | None) -> dict:
    """The firmware default with the given fields over it (unknown fields are ignored)."""
    out = default_set(scenario)
    for key, v in (params or {}).items():
        if key in ("knots_pct", "xk"):
            out[key] = [float(a) for a in v]
        elif key in SCALARS and v is not None:
            out[key] = float(v)
    return out


def validate(scenario, params: dict) -> str | None:
    """The firmware's own validate(): None when the set would be accepted, else its message."""
    return _set(None, scenario, full_set(scenario, params))


def _set(handle, scenario, p: dict) -> str | None:
    if len(p["knots_pct"]) != 11 or len(p["xk"]) != 11:
        return "knots_pct and xk need 11 values"
    err = ctypes.create_string_buffer(160)
    rc = lib().mpc_set(handle, scenario_index(scenario), (ctypes.c_float * 11)(*p["knots_pct"]),
                       (ctypes.c_float * 11)(*p["xk"]),
                       (ctypes.c_float * len(SCALARS))(*(p[n] for n in SCALARS)), err, len(err))
    return (err.value.decode() or "invalid") if rc else None


class Core:
    """One controller instance (hpn::mpc::Controller)."""

    def __init__(self, sets: dict | None = None):
        """sets: {scenario name: parameter fields}; missing scenarios keep the firmware defaults."""
        self.L = lib()
        self.h = self.L.mpc_new()
        self._t = ctypes.c_float()
        self.sets = {sc: full_set(sc, (sets or {}).get(sc)) for sc in SCENARIOS}
        self.scenario = None
        self.use("floor")

    def __del__(self):
        try:
            self.L.mpc_free(self.h)
        except Exception:  # noqa: BLE001
            pass

    def use(self, scenario) -> None:
        """Load a scenario's set (the controller does it when the radiator pumps change)."""
        name = SCENARIOS[scenario_index(scenario)]
        if name != self.scenario:
            err = _set(self.h, name, self.sets[name])
            if err:
                raise ValueError(f"{name}: {err}")
            self.scenario = name

    def request_mode(self, mode: int, now: float) -> None:
        self.L.mpc_request_mode(self.h, mode, now)

    def on_homed(self, now: float) -> None:
        self.L.mpc_on_homed(self.h, now)

    def tick(self, now, pos, target, t_inc, t_ret, t_fwd, setpoint, pmin, pmax, travel_s, overshoot_delta,
             valid=(True, True, True)) -> tuple[int, float]:
        """pos, target, pmin, pmax as fractions 0..1. Returns (command kind, target)."""
        k = self.L.mpc_tick(self.h, now, pos, target, t_inc, t_ret, t_fwd, int(valid[0]), int(valid[1]),
                            int(valid[2]), setpoint, pmin, pmax, travel_s, overshoot_delta,
                            scenario_index(self.scenario), ctypes.byref(self._t))
        return k, self._t.value

    def status(self) -> dict:
        f = (ctypes.c_float * 5)()
        i = (ctypes.c_int * 7)()
        self.L.mpc_status(self.h, f, i)
        return dict(mode=MODES.get(i[0], "?"), anomaly=ANOMALIES.get(i[1], "?"), retries=i[2], plans=i[3],
                    triggered=i[4], anomalies=i[5], boost=bool(i[6]), bias=f[0], tinc_water=f[1],
                    model_fwd=f[2], cost=f[3], stable_s=f[4])
