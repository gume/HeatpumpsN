#!/usr/bin/env python3
"""
step2_hx_dynamics_v63.py
Phase 2 (Version 6.3): Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)
Hardware-in-the-Loop (HIL) Floor Heating Simulator Project

Version 6.3 is the response to an external audit of v6.2.  It keeps the v6
physics but changes how the model is identified, evaluated and reported:

1. Phase 0 freezes a new telemetry-only holdout (VAL-8, VAL-9) and the numeric
   gates before any model code is touched.  The observed v6.2 holdout
   (2026-02-26) is relabelled "observed holdout (seen in v6.2)" and cannot
   support a sign-off.
2. FIT-1 is shortened to remove its overlap with VAL-4 and its warm-up.
   Disjointness, including 30-minute warm-ups, is asserted programmatically.
3. tau_idle and tau_rad_off are fitted per contiguous segment by exponential
   least squares instead of a quantisation-biased one-step ratio; the room
   sensor offset is identified on FIT data.
4. Effectiveness and tau_hx/eps_A are selected on inputs only, with a settled
   hydraulic filter, and stale FIT samples are excluded.
5. Pump powers follow the same event-style staleness rule as the burner and
   pump modulation, mirrored exactly in Python and C++.
6. The search grids include zero floors, are extended away from boundaries,
   and tau_cold_front is constrained to >= DT.  The cold-start front is fitted
   on all TUNE cold starts over the measured transient length.
7. Mid-run cutoffs require a relight; the remaining run-down is part of the
   final shutdown.
8. Python n_cold is clamped to 32 exactly as C++, the C++ majority buffer is a
   constexpr MAX_WINDOW, the harness prints %.15g, and parity is checked on two
   parameter sets to below 1e-9 degC.
9. The report is generated entirely from computed values and prints the frozen
   gate/holdout/parameter hashes and the commit id.

Run with:
    uv run python simulation/step2_hx_dynamics_v63.py
or, with the system interpreter:
    python3 simulation/step2_hx_dynamics_v63.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Portable environment bootstrap
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("HEATPUMPS_OUT_DIR", str(BASE_DIR / "simulation")))
DB_PATH = Path(os.environ.get("HEATPUMPS_DB_PATH", str(BASE_DIR / "analysis" / "heatpumps.duckdb")))
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _relative_db_path() -> str:
    """Machine-independent database path for JSON artifacts."""
    try:
        return str(DB_PATH.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        return DB_PATH.name


os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl_config")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import duckdb
import numpy as np
import pandas as pd
from scipy.ndimage import median_filter
from scipy.optimize import minimize, minimize_scalar

# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------
DT = 10.0  # simulation step [s]
WARMUP_MIN = 30.0  # causal warm-up buffer for every episode [min]
WARMUP_STEPS = int(round(WARMUP_MIN * 60.0 / DT))
MAX_COLD_RING_N = 32  # identical limit in the generated C++ header

# Telemetry validity thresholds.  The boiler flow temperature is a
# continuously-updated analog signal, so a stale tb is a reliable gateway-drop
# indicator.  Burner, pump modulation and pump powers are event-driven and can
# legitimately stay unchanged for hours; they are stale only when their own age
# exceeds the loose threshold *and* the tb heartbeat is stale at the same time.
TB_MAX_AGE_S = 900.0
EVT_MAX_AGE_S = 14400.0

# v6.3 artifact filenames (the v6 ones are intentionally left untouched).
VERSION = "6.3"
PARAMS_JSON_NAME = "step2_params_v63.json"
REPORT_NAME = "step2_hx_dynamics_report_v63.md"
HPP_NAME = "step2_hx_dynamics_v63.hpp"
HARNESS_NAME = "step2_hx_parity_harness_v63.cpp"
HARNESS_BIN_NAME = "step2_hx_parity_harness_v63"
PARITY_CSV_NAME = "step2_hx_parity_input_v63.csv"
PARITY_PARAMS_NAME = "step2_hx_parity_params_v63.csv"
PLOT_NAME = "tune1_diagnostic_overlay_v63.png"
HOLDOUT_PATH = Path(__file__).resolve().parent / "holdout_v63.json"
GATES_PATH = Path(__file__).resolve().parent / "gates_v63.json"
FREEZE_PATH = Path(__file__).resolve().parent / "freeze_v63.json"

# Cold-start trigger: a prolonged idle followed by a firing with a cold
# manifold and a large boiler-to-manifold lift.
L_IDLE_TRIGGER_S = 1800.0
COLD_EXIT_TEMP_C = 28.0
COLD_EXIT_TAU_MULT = 5.0

# Frozen v5 constants, used only for the historical v3/v4/v5 comparison rows.
V5_FROZEN = {
    "tau_hx": 77.6754247909429,
    "eps_A": 0.9450097116769788,
    "eps_B1": 0.9510234288280979,
    "eps_B2": 0.9410733629621009,
    "eps_C": 0.915396748779781,
    "dhw_delay_s": 90.0,
    "tau_dhw_fast": 87.62046435466691,
    "post_dhw_delay_s": 90.0,
    "L_pump_off": 70.0,
    "L_burn_off": 30.0,
    "tau_burn_off": 2400.0,
    "tau_cutoff": 150.0,
    "tau_ambient": 36000.0,
    "tau_rad_off": 2130.0,
    "L_cold_start": 0.0,
    "tau_cold_front": 0.0,
}

# Episode registry.  Times are local Europe/Vienna (the database stores tz-aware
# timestamps, so the strings are unambiguous).
EPISODES = {
    "FIT-1": dict(
        name="FIT-1 Space-Heating Identification (shortened)",
        kind="FIT", t_start="2026-01-14 00:00:00+01:00", t_end="2026-01-14 04:00:00+01:00",
        role="tau_hx / eps_A identification (ends 30 min before VAL-4 warm-up)",
    ),
    "FIT-DHW": dict(
        name="FIT-DHW Divert Identification",
        kind="FIT", t_start="2025-12-03 08:18:00+01:00", t_end="2025-12-03 08:45:00+01:00",
        role="dhw_delay_s / tau_dhw_fast identification",
    ),
    "TUNE-1": dict(
        name="TUNE-1 Cold Start + Cutoffs + Final Shutdown",
        kind="TUNE", t_start="2025-12-27 16:06:00+01:00", t_end="2025-12-27 20:58:00+01:00",
        role="Cutoff / cold-start / post-DHW tuning",
    ),
    "TUNE-2": dict(
        name="TUNE-2 DHW Divert + Return",
        kind="TUNE", t_start="2026-01-01 14:50:00+01:00", t_end="2026-01-01 15:25:00+01:00",
        role="DHW return / post-DHW tuning (in-partition)",
    ),
    "TUNE-3": dict(
        name="TUNE-3 Model-Selection: Cold Start, Cutoffs & Post-Heating Idle",
        kind="TUNE", t_start="2026-02-08 04:27:00+01:00", t_end="2026-02-08 10:11:00+01:00",
        role="Model-selection episode (former VAL-5); idle/cutoff structure selection",
    ),
    "VAL-1": dict(
        name="VAL-1 Regime A Heated Run 2",
        kind="VAL", t_start="2026-01-15 00:00:00+01:00", t_end="2026-01-15 05:00:00+01:00",
        role="Untouched validation",
    ),
    "VAL-2": dict(
        name="VAL-2 Regime B Combined Run 1",
        kind="VAL", t_start="2026-02-19 14:40:00+01:00", t_end="2026-02-19 17:25:00+01:00",
        role="Untouched validation",
    ),
    "VAL-3": dict(
        name="VAL-3 Regime B Combined Run 2",
        kind="VAL", t_start="2026-01-13 05:00:00+01:00", t_end="2026-01-13 07:35:00+01:00",
        role="Untouched validation",
    ),
    "VAL-4": dict(
        name="VAL-4 Hydraulic Transition A->B",
        kind="VAL", t_start="2026-01-14 05:00:00+01:00", t_end="2026-01-14 07:30:00+01:00",
        role="Untouched validation",
    ),
    "VAL-5": dict(
        name="VAL-5 Observed Holdout (seen in v6.2)",
        kind="VAL", t_start="2026-02-26 00:00:00+01:00", t_end="2026-02-26 12:00:00+01:00",
        role="Observed holdout (seen in v6.2) - reported, cannot support sign-off",
    ),
    "VAL-6": dict(
        name="VAL-6 Circulating DHW Divert + Return",
        kind="VAL", t_start="2026-02-18 05:50:00+01:00", t_end="2026-02-18 06:55:00+01:00",
        role="New untouched validation (replaces old VAL-6)",
    ),
    "VAL-7": dict(
        name="VAL-7 DHW Priority Divert (Idle)",
        kind="VAL", t_start="2026-01-03 03:20:00+01:00", t_end="2026-01-03 03:50:00+01:00",
        role="Untouched validation",
    ),
}
FIT_WINDOW = dict(t_start="2025-12-01 22:00:00+01:00", t_end="2025-12-24 23:59:00+01:00")


def load_holdout_episodes() -> dict:
    """Load the Phase 0 telemetry-only holdout registry.

    The selector script owns these windows; v6.3 only reads them.  They are
    added as ``VAL-8``/``VAL-9`` with kind ``HOLDOUT`` so that they are never
    part of the FIT or TUNE loops.
    """
    assert HOLDOUT_PATH.exists(), (
        f"Missing {HOLDOUT_PATH.name}. Run simulation/select_holdout_v63.py "
        "and commit the holdout before running v6.3."
    )
    payload = json.loads(HOLDOUT_PATH.read_text())
    out = {}
    for eid, ep in payload["episodes"].items():
        out[eid] = dict(
            name=ep["name"], kind="HOLDOUT",
            t_start=ep["t_start"], t_end=ep["t_end"], role=ep["role"],
        )
    assert out, "Holdout registry is empty"
    return out


EPISODES.update(load_holdout_episodes())
HOLDOUT_IDS = tuple(eid for eid, meta in EPISODES.items()
                    if meta["kind"] == "HOLDOUT")
OBSERVED_VAL_IDS = ("VAL-1", "VAL-2", "VAL-3", "VAL-4", "VAL-5",
                    "VAL-6", "VAL-7")


def sensor_resolutions(con: duckdb.DuckDBPyConnection,
                       limit: int = 200000) -> dict:
    """Measure the raw quantisation of the four temperature sensors in-run.

    The resolution is the smallest non-zero absolute difference between
    consecutive raw rows, so it is a property of the telemetry and not a
    hard-coded report constant.
    """
    specs = dict(
        mixer_incoming="mixer_incoming_temperature",
        mixer_return="mixer_return_temperature",
        room="temperature_humidity_sensor_d281_temperature",
        boiler_flow="boiler_curflowtemp",
    )
    out = {}
    for key, table in specs.items():
        d = con.execute(
            f"SELECT v FROM {table} ORDER BY t LIMIT {int(limit)}").df()
        vals = d["v"].values.astype(float)
        diffs = np.abs(np.diff(vals))
        pos = diffs[diffs > 1e-12]
        out[key] = float(np.min(pos)) if len(pos) else float("nan")
        out[key + "_rows"] = int(len(vals))
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, text=True).strip()
    except Exception:
        return "unknown"


def print_banner(text: str) -> None:
    print("\n" + "=" * 96)
    print(f" {text.upper()}")
    print("=" * 96)


def print_section(text: str) -> None:
    print("\n" + "-" * 96)
    print(f" {text}")
    print("-" * 96)


# ---------------------------------------------------------------------------
# Database loading (no bfill, warm-up buffer, explicit staleness)
# ---------------------------------------------------------------------------
def load_episode(con: duckdb.DuckDBPyConnection, t_start: str, t_end: str,
                 warmup_min: float = WARMUP_MIN) -> pd.DataFrame:
    """Load a 10 s episode with a causal warm-up buffer.

    The grid starts `warmup_min` before `t_start`; the caller evaluates only
    rows from `WARMUP_STEPS` onwards.  ASOF joins guarantee a value at or
    before every grid point.  No backward fill is ever used.
    """
    q = f"""
    WITH grid AS (
        SELECT unnest(generate_series(
            TIMESTAMPTZ '{t_start}' - INTERVAL '{int(round(warmup_min))} MINUTE',
            TIMESTAMPTZ '{t_end}',
            INTERVAL 10 SECOND
        )) AS t
    )
    SELECT
        g.t,
        t_b.v   AS t_boiler,
        t_inc.v AS t_inc,
        t_ret.v AS t_ret,
        t_fwd.v AS t_fwd,
        t_rm.v  AS t_room,
        m_st.v  AS mixer_state,
        coalesce(bp.v, 0.0)   AS burner_pct,
        coalesce(pmod.v, 0.0) AS pump_mod,
        coalesce(p1.v, 0.0)   AS p1_w,
        coalesce(p2.v, 0.0)   AS p2_w,
        coalesce(p3.v, 0.0)   AS p3_w,
        coalesce(ww.v, 0.0)   AS ww_flag,
        coalesce(ha.v, 0.0)   AS ha_flag,
        (epoch(g.t) - epoch(bp.t))   AS bp_age_s,
        (epoch(g.t) - epoch(pmod.t)) AS pmod_age_s,
        (epoch(g.t) - epoch(t_b.t))  AS tb_age_s,
        (epoch(g.t) - epoch(t_ret.t)) AS tret_age_s,
        (epoch(g.t) - epoch(p1.t))   AS p1_age_s,
        (epoch(g.t) - epoch(p2.t))   AS p2_age_s,
        (epoch(g.t) - epoch(p3.t))   AS p3_age_s,
        (epoch(g.t) - epoch(t_inc.t)) AS tinc_age_s
    FROM grid g
    ASOF JOIN boiler_curflowtemp t_b             ON g.t >= t_b.t
    ASOF JOIN mixer_incoming_temperature t_inc   ON g.t >= t_inc.t
    ASOF JOIN mixer_return_temperature t_ret     ON g.t >= t_ret.t
    ASOF JOIN mixer_forward_temperature t_fwd    ON g.t >= t_fwd.t
    ASOF JOIN temperature_humidity_sensor_d281_temperature t_rm ON g.t >= t_rm.t
    ASOF JOIN mixer_state m_st                   ON g.t >= m_st.t
    ASOF JOIN boiler_curburnpow bp                ON g.t >= bp.t
    ASOF JOIN boiler_heatingpumpmod pmod          ON g.t >= pmod.t
    ASOF JOIN heatpumps_pump_1_power p1           ON g.t >= p1.t
    ASOF JOIN heatpumps_pump_2_power p2           ON g.t >= p2.t
    ASOF JOIN heatpumps_pump_3_power p3           ON g.t >= p3.t
    ASOF JOIN boiler_wwcharging ww                ON g.t >= ww.t
    ASOF JOIN boiler_heatingactive ha             ON g.t >= ha.t
    ORDER BY g.t;
    """
    df = con.execute(q).df()
    assert len(df) > 0, f"Empty episode {t_start} -> {t_end}"
    assert not df.isna().any().any(), (
        f"NaN detected in episode {t_start} -> {t_end}: "
        f"{df.isna().sum()[df.isna().sum() > 0].to_dict()}"
    )
    anchor_index = min(len(df) - 1, max(0, int(round(warmup_min * 60.0 / DT))))
    anchor_age = float(df["tinc_age_s"].iloc[anchor_index])
    assert anchor_age <= TB_MAX_AGE_S, (
        f"Stale re-anchor ground truth in episode {t_start} -> {t_end}: "
        f"tinc_age_s[{anchor_index}]={anchor_age:.0f} s > {TB_MAX_AGE_S:.0f} s"
    )
    return df


def load_fit_1m(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load the 1-minute FIT window used for regime ratios and tau_rad_off."""
    q = f"""
    WITH grid AS (
        SELECT unnest(generate_series(
            TIMESTAMPTZ '{FIT_WINDOW["t_start"]}',
            TIMESTAMPTZ '{FIT_WINDOW["t_end"]}',
            INTERVAL 1 MINUTE
        )) AS t
    )
    SELECT
        g.t,
        coalesce(p1.v, 0.0)   AS p1_w,
        coalesce(p2.v, 0.0)   AS p2_w,
        coalesce(p3.v, 0.0)   AS p3_w,
        coalesce(ww.v, 0.0)   AS ww_flag,
        coalesce(bp.v, 0.0)   AS burner_pct,
        coalesce(pmod.v, 0.0) AS pump_mod,
        t_b.v   AS t_boiler,
        t_inc.v AS t_inc,
        t_ret.v AS t_ret,
        m_st.v  AS mixer_state,
        t_rm.v  AS t_room,
        (epoch(g.t) - epoch(bp.t))    AS bp_age_s,
        (epoch(g.t) - epoch(pmod.t))  AS pmod_age_s,
        (epoch(g.t) - epoch(t_b.t))   AS tb_age_s,
        (epoch(g.t) - epoch(t_ret.t)) AS tret_age_s,
        (epoch(g.t) - epoch(p1.t))    AS p1_age_s,
        (epoch(g.t) - epoch(p2.t))    AS p2_age_s,
        (epoch(g.t) - epoch(p3.t))    AS p3_age_s,
        (epoch(g.t) - epoch(t_inc.t)) AS tinc_age_s
    FROM grid g
    ASOF JOIN heatpumps_pump_1_power p1  ON g.t >= p1.t
    ASOF JOIN heatpumps_pump_2_power p2  ON g.t >= p2.t
    ASOF JOIN heatpumps_pump_3_power p3  ON g.t >= p3.t
    ASOF JOIN boiler_wwcharging ww       ON g.t >= ww.t
    ASOF JOIN boiler_curburnpow bp       ON g.t >= bp.t
    ASOF JOIN boiler_heatingpumpmod pmod ON g.t >= pmod.t
    ASOF JOIN boiler_curflowtemp t_b     ON g.t >= t_b.t
    ASOF JOIN mixer_incoming_temperature t_inc ON g.t >= t_inc.t
    ASOF JOIN mixer_return_temperature t_ret   ON g.t >= t_ret.t
    ASOF JOIN mixer_state m_st           ON g.t >= m_st.t
    ASOF JOIN temperature_humidity_sensor_d281_temperature t_rm ON g.t >= t_rm.t
    ORDER BY g.t;
    """
    df = con.execute(q).df()
    assert not df.isna().any().any(), "NaN detected in FIT 1-minute grid"
    return df


def freshness_mask(df: pd.DataFrame) -> np.ndarray:
    """v6.3 effective staleness rule for a loaded frame.

    Analog ages (tb, tret) are limited by ``TB_MAX_AGE_S``.  Event-driven ages
    (burner, pump modulation and all pump powers) are stale only when their own
    age exceeds ``EVT_MAX_AGE_S`` *and* the tb heartbeat is also stale.
    """
    tb = df["tb_age_s"].values.astype(float)
    tret = df["tret_age_s"].values.astype(float)
    analog = (tb <= TB_MAX_AGE_S) & (tret <= TB_MAX_AGE_S)
    ev = np.zeros(len(df), dtype=bool)
    for col in ("bp_age_s", "pmod_age_s", "p1_age_s", "p2_age_s", "p3_age_s"):
        ev |= df[col].values.astype(float) > EVT_MAX_AGE_S
    return analog & ~(ev & (tb > TB_MAX_AGE_S))


def causal_majority_np(arr: np.ndarray, window: int = 31) -> np.ndarray:
    """Vectorised trailing causal majority (used only for identification)."""
    s = pd.Series(np.asarray(arr, dtype=float))
    rs = s.rolling(window, min_periods=1).sum().values
    rc = s.rolling(window, min_periods=1).count().values
    return (2.0 * rs >= rc)


# ---------------------------------------------------------------------------
# FIT identification
# ---------------------------------------------------------------------------
def identify_fit_ratios_legacy(df_fit: pd.DataFrame) -> dict:
    """Effectiveness ratios on the FIT window (1-24 Dec), plus tau_rad_off
    and tau_idle."""
    p1 = median_filter((df_fit["p1_w"].values >= 2.0).astype(int), 5).astype(bool)
    p2 = median_filter((df_fit["p2_w"].values >= 2.0).astype(int), 5).astype(bool)
    p3 = median_filter((df_fit["p3_w"].values >= 2.0).astype(int), 5).astype(bool)
    ww = df_fit["ww_flag"].values >= 0.5
    rad = p1 | p2
    rad_cnt = p1.astype(int) + p2.astype(int)

    regime = np.full(len(df_fit), "other", dtype=object)
    regime[ww] = "D"
    regime[~ww & p3 & rad] = "B"
    regime[~ww & p3 & ~rad] = "A"
    regime[~ww & ~p3 & rad] = "C"
    regime[~ww & ~p3 & ~rad & (df_fit["burner_pct"].values <= 0.0)] = "E"

    tb = df_fit["t_boiler"].values
    tinc = df_fit["t_inc"].values
    tret = df_fit["t_ret"].values
    bp = df_fit["burner_pct"].values
    active = (~ww) & (bp > 5.0) & (tinc >= 40.0) & ((tinc - tret) >= 10.0)
    denom = tb - tret
    eff = np.where(np.abs(denom) > 1e-9, (tinc - tret) / denom, np.nan)
    dt_hx = tb - tinc

    def stat(mask):
        m = active & mask & np.isfinite(eff)
        return dict(
            count=int(m.sum()),
            eff_mean=float(np.nanmean(eff[m])) if m.any() else float("nan"),
            dt_mean=float(np.nanmean(dt_hx[m])) if m.any() else float("nan"),
        )

    a = stat(regime == "A")
    b1 = stat((regime == "B") & (rad_cnt == 1))
    b2 = stat((regime == "B") & (rad_cnt == 2))
    c = stat(regime == "C")
    assert a["count"] > 100 and b1["count"] > 50 and b2["count"] > 20 and c["count"] > 20, (
        "Insufficient FIT samples for regime ratios"
    )
    ratio_B1 = b1["eff_mean"] / a["eff_mean"]
    ratio_B2 = b2["eff_mean"] / a["eff_mean"]
    ratio_C = c["eff_mean"] / a["eff_mean"]

    # tau_rad_off: one-step cooling ratio for radiator-only pump-off segments.
    # This is identified on FIT only; it is never tuned on TUNE or VAL.
    x = tinc
    r = tret
    m = (df_fit["pump_mod"].values <= 0.0) & rad & (~p3) & (~ww)
    m[:-1] &= m[1:]
    d0 = x[:-1] - r[:-1]
    d1 = x[1:] - r[1:]
    sel = m[:-1] & (d0 > 2.0) & (d1 > 0.0) & (d1 < d0)
    if int(sel.sum()) >= 20:
        ratio_min = float(np.median(d1[sel] / d0[sel]))
        ratio_min = min(max(ratio_min, 1e-4), 0.9999)
        tau_rad_off = float(-60.0 / math.log(ratio_min))
    else:
        ratio_min = float("nan")
        tau_rad_off = 2130.0

    # tau_idle: one-step cooling ratio for hot-manifold pure-idle segments.
    # Only samples with no secondary pump, no DHW and a manifold well above
    # room temperature are used.  This is identified on FIT only and replaces
    # the v5 tau_ambient design constant, which was derived from cold DHW idle
    # episodes already close to room equilibrium.
    xi = tinc
    ri = df_fit["t_room"].values
    mi = (~p1) & (~p2) & (~p3) & (~ww) & ((xi - ri) > 10.0)
    mi[:-1] &= mi[1:]
    di0 = xi[:-1] - ri[:-1]
    di1 = xi[1:] - ri[1:]
    sel_i = mi[:-1] & (di0 > 10.0) & (di1 > 0.0) & (di1 < di0)
    if int(sel_i.sum()) >= 20:
        ratio_idle = float(np.median(di1[sel_i] / di0[sel_i]))
        ratio_idle = min(max(ratio_idle, 1e-4), 0.9999)
        tau_idle = float(-60.0 / math.log(ratio_idle))
    else:
        ratio_idle = float("nan")
        tau_idle = 36000.0

    # Hydraulic decoupling correlations, recomputed on FIT-only Regime A.
    mA = active & (regime == "A") & np.isfinite(eff)
    mstate = df_fit["mixer_state"].values.astype(float)
    if int(mA.sum()) > 10:
        r_mstate_tinc = float(np.corrcoef(mstate[mA], tinc[mA])[0, 1])
        r_mstate_dthx = float(np.corrcoef(mstate[mA], dt_hx[mA])[0, 1])
        r_tb_tinc = float(np.corrcoef(tb[mA], tinc[mA])[0, 1])
    else:
        r_mstate_tinc = r_mstate_dthx = r_tb_tinc = float("nan")

    return dict(
        regime_counts={k: int((regime == k).sum())
                       for k in ("A", "B", "C", "D", "E", "other")},
        active_count=int(active.sum()),
        A=a, B1=b1, B2=b2, C=c,
        ratio_B1=float(ratio_B1), ratio_B2=float(ratio_B2), ratio_C=float(ratio_C),
        tau_rad_off=tau_rad_off, tau_rad_ratio_per_min=ratio_min,
        tau_rad_samples=int(sel.sum()),
        tau_idle=tau_idle, tau_idle_ratio_per_min=ratio_idle,
        tau_idle_samples=int(sel_i.sum()),
        fit_window=FIT_WINDOW,
        fit_regime_a_correlations=dict(
            r_mstate_tinc=r_mstate_tinc,
            r_mstate_dthx=r_mstate_dthx,
            r_tb_tinc=r_tb_tinc,
            samples=int(mA.sum()),
        ),
    )


def identify_hx_fit1_legacy(con: duckdb.DuckDBPyConnection) -> dict:
    """Fit tau_hx and eps_A on FIT-1 (10 s, active heating only)."""
    df = load_episode(con, EPISODES["FIT-1"]["t_start"], EPISODES["FIT-1"]["t_end"], warmup_min=0.0)
    tb = df["t_boiler"].values
    tinc = df["t_inc"].values
    tret = df["t_ret"].values
    bp = df["burner_pct"].values
    mask = (bp > 5.0) & (tinc >= 40.0) & ((tinc - tret) >= 10.0)
    assert int(mask.sum()) > 200

    def loss(p):
        tau, eps = float(p[0]), float(p[1])
        alpha = DT / (tau + DT)
        sim = np.empty(len(tb))
        sim[0] = tinc[0]
        for k in range(1, len(tb)):
            target = tret[k] + eps * (tb[k] - tret[k])
            sim[k] = (1.0 - alpha) * sim[k - 1] + alpha * target
        return float(np.mean((sim[mask] - tinc[mask]) ** 2))

    res = minimize(loss, [70.0, 0.94], bounds=[(10.0, 300.0), (0.80, 0.99)], method="L-BFGS-B")
    tau_hx = float(res.x[0])
    eps_A = float(res.x[1])
    assert 20.0 <= tau_hx <= 200.0 and 0.85 <= eps_A <= 0.99
    return dict(tau_hx=tau_hx, eps_A=eps_A, mse=float(res.fun),
                rmse=float(math.sqrt(res.fun)), samples=int(mask.sum()))


def _run_segments(mask: np.ndarray) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        out.append((i, j + 1))
        i = j + 1
    return out


def _fit_tau_segment(t_min: np.ndarray, excess: np.ndarray) -> tuple[float, float, float]:
    """Least-squares fit of ``excess(t) = y0 * exp(-t/tau)`` on all samples.

    ``t_min`` is in minutes and ``tau`` is in seconds, so the exponent uses
    ``60 * t_min / tau``.
    """
    def sse_for(tau: float) -> float:
        e = np.exp(-60.0 * t_min / tau)
        denom = float(np.dot(e, e))
        if denom <= 0.0:
            return 1e18
        y0 = float(np.dot(excess, e)) / denom
        if y0 <= 0.0:
            return 1e18
        return float(np.sum((excess - y0 * e) ** 2))

    res = minimize_scalar(sse_for, bounds=(300.0, 2.0e6), method="bounded")
    tau = float(res.x)
    e = np.exp(-60.0 * t_min / tau)
    y0 = float(np.dot(excess, e)) / max(float(np.dot(e, e)), 1e-12)
    return tau, float(res.fun), y0


def fit_exponential_segments(df: pd.DataFrame, mask: np.ndarray,
                             ref: np.ndarray, min_len: int = 20,
                             min_excess: float = 10.0) -> list[dict]:
    """Fit one tau per contiguous qualifying segment (>= 20 minutes)."""
    out = []
    for a, b in _run_segments(mask):
        if b - a < min_len:
            continue
        y = df["t_inc"].values[a:b].astype(float)
        r = float(np.mean(ref[a:b]))
        excess = y - r
        if excess[0] < min_excess or np.any(excess <= 0.0):
            continue
        if excess[-1] > 0.95 * excess[0]:
            continue  # no resolvable decay over the segment
        t_min = np.arange(b - a) * 1.0
        tau, sse, y0 = _fit_tau_segment(t_min, excess)
        out.append(dict(start=int(a), stop=int(b), samples=int(b - a),
                        tau_s=tau, sse=sse, y0_c=y0))
    return out


def _tau_stats(fits: list[dict]) -> dict:
    taus = np.array([f["tau_s"] for f in fits], dtype=float)
    if len(taus) == 0:
        return dict(median_s=float("nan"), q25_s=float("nan"),
                    q75_s=float("nan"), iqr_s=float("nan"), segments=0)
    return dict(median_s=float(np.median(taus)),
                q25_s=float(np.percentile(taus, 25)),
                q75_s=float(np.percentile(taus, 75)),
                iqr_s=float(np.percentile(taus, 75) - np.percentile(taus, 25)),
                segments=int(len(taus)))


def identify_fit_ratios(df_fit: pd.DataFrame) -> dict:
    """v6.3 FIT identification.

    * input-only effectiveness mask with 5-minute pump-state stability;
    * per-segment exponential fits for tau_idle and tau_rad_off;
    * a fitted room-sensor offset for the idle reference;
    * a settled hydraulic-correlation filter.
    """
    n = len(df_fit)
    fresh = freshness_mask(df_fit)
    p1 = median_filter((df_fit["p1_w"].values >= 2.0).astype(int), 5).astype(bool)
    p2 = median_filter((df_fit["p2_w"].values >= 2.0).astype(int), 5).astype(bool)
    p3 = median_filter((df_fit["p3_w"].values >= 2.0).astype(int), 5).astype(bool)
    p1r = (df_fit["p1_w"].values >= 2.0).astype(int)
    p2r = (df_fit["p2_w"].values >= 2.0).astype(int)
    p3r = (df_fit["p3_w"].values >= 2.0).astype(int)
    ww = df_fit["ww_flag"].values >= 0.5
    rad = p1 | p2
    rad_cnt = p1.astype(int) + p2.astype(int)

    regime = np.full(n, "other", dtype=object)
    regime[ww] = "D"
    regime[~ww & p3 & rad] = "B"
    regime[~ww & p3 & ~rad] = "A"
    regime[~ww & ~p3 & rad] = "C"
    regime[~ww & ~p3 & ~rad & (df_fit["burner_pct"].values <= 0.0)] = "E"

    tb = df_fit["t_boiler"].values
    tinc = df_fit["t_inc"].values
    tret = df_fit["t_ret"].values
    bp = df_fit["burner_pct"].values
    denom = tb - tret
    eff = np.where(np.abs(denom) > 1e-9, (tinc - tret) / denom, np.nan)
    dt_hx = tb - tinc

    # 5-minute stability of every pump state (inputs only).
    stable = np.ones(n, dtype=bool)
    win = 5
    for arr in (p1r, p2r, p3r):
        for k in range(win, n):
            stable[k] = stable[k] and bool(np.all(arr[k - win:k + 1] == arr[k]))

    new_active = (fresh & (~ww) & (bp > 5.0) & ((tb - tret) > 10.0) & stable)
    old_active = (fresh & (~ww) & (bp > 5.0) & (tinc >= 40.0)
                  & ((tinc - tret) >= 10.0))

    def stat(mask, active_mask):
        m = active_mask & mask & np.isfinite(eff)
        return dict(
            count=int(m.sum()),
            eff_mean=float(np.nanmean(eff[m])) if m.any() else float("nan"),
            dt_mean=float(np.nanmean(dt_hx[m])) if m.any() else float("nan"),
        )

    def ratios(active_mask):
        a = stat(regime == "A", active_mask)
        b1 = stat((regime == "B") & (rad_cnt == 1), active_mask)
        b2 = stat((regime == "B") & (rad_cnt == 2), active_mask)
        c = stat(regime == "C", active_mask)
        assert a["count"] > 50 and b1["count"] > 20 and b2["count"] > 10 \
            and c["count"] > 10, "Insufficient FIT samples for regime ratios"
        return dict(
            A=a, B1=b1, B2=b2, C=c,
            ratio_B1=float(b1["eff_mean"] / a["eff_mean"]),
            ratio_B2=float(b2["eff_mean"] / a["eff_mean"]),
            ratio_C=float(c["eff_mean"] / a["eff_mean"]),
        )

    new_r = ratios(new_active)
    old_r = ratios(old_active)

    # --- tau_rad_off: per-segment exponential fits, radiator-only pump-off.
    rad_mask = fresh & (df_fit["pump_mod"].values <= 0.0) & rad & (~p3) & (~ww)
    rad_fits = fit_exponential_segments(df_fit, rad_mask, tret)
    rad_stats = _tau_stats(rad_fits)

    # --- tau_idle: per-segment exponential fits; identify T_amb = T_room+delta.
    idle_mask = fresh & (~p1) & (~p2) & (~p3) & (~ww)
    room = df_fit["t_room"].values.astype(float)
    delta0_fits = fit_exponential_segments(df_fit, idle_mask, room)
    sse0 = float(sum(f["sse"] for f in delta0_fits))
    best_delta, best_sse, best_fits = 0.0, sse0, delta0_fits
    delta_lo, delta_hi = -5.0, 5.0
    for delta in np.arange(delta_lo, delta_hi + 0.01, 0.25):
        fits = fit_exponential_segments(df_fit, idle_mask, room + delta)
        sse = float(sum(f["sse"] for f in fits))
        if sse < best_sse:
            best_delta, best_sse, best_fits = float(delta), sse, fits
    # A boundary optimum means the offset is absorbing model mismatch, not
    # identifying a sensor bias; keep the zero offset in that case.
    interior = (best_delta > delta_lo + 1e-9) and (best_delta < delta_hi - 1e-9)
    adopt_delta = bool(interior and best_sse < sse0)
    if not adopt_delta:
        best_delta, best_fits = 0.0, delta0_fits
    idle_stats = _tau_stats(best_fits)

    # Fall back to the legacy one-step estimate when no qualifying segment
    # exists; this is flagged explicitly in the report.
    def _fallback(tau_value: float) -> float:
        return float(tau_value) if math.isfinite(float(tau_value)) else 2130.0

    # Old one-step ratios, kept for the diagnostic comparison.
    x, r = tinc, tret
    m = rad_mask.copy()
    m[:-1] &= m[1:]
    d0 = x[:-1] - r[:-1]
    d1 = x[1:] - r[1:]
    sel = m[:-1] & (d0 > 2.0) & (d1 > 0.0) & (d1 < d0)
    old_rad_tau = float("nan")
    old_rad_ratio = float("nan")
    if int(sel.sum()) >= 20:
        old_rad_ratio = float(np.median(d1[sel] / d0[sel]))
        old_rad_ratio = min(max(old_rad_ratio, 1e-4), 0.9999)
        old_rad_tau = float(-60.0 / math.log(old_rad_ratio))

    mi = idle_mask.copy()
    mi[:-1] &= mi[1:]
    di0 = x[:-1] - room[:-1]
    di1 = x[1:] - room[1:]
    sel_i = mi[:-1] & (di0 > 10.0) & (di1 > 0.0) & (di1 < di0)
    old_idle_tau = float("nan")
    old_idle_ratio = float("nan")
    if int(sel_i.sum()) >= 20:
        old_idle_ratio = float(np.median(di1[sel_i] / di0[sel_i]))
        old_idle_ratio = min(max(old_idle_ratio, 1e-4), 0.9999)
        old_idle_tau = float(-60.0 / math.log(old_idle_ratio))

    # --- Settled hydraulic filter: regime stable >=10 min, mixer unchanged
    # for >=5 min.
    settled = np.zeros(n, dtype=bool)
    for k in range(10, n):
        settled[k] = bool(np.all(regime[k - 10:k + 1] == regime[k]))
    mstate = df_fit["mixer_state"].values.astype(float)
    for k in range(5, n):
        settled[k] = settled[k] and bool(np.all(mstate[k - 5:k + 1] == mstate[k]))
    ms = settled & np.isfinite(eff)
    full = np.isfinite(eff) & np.isfinite(mstate)

    def corr(a, b, mask):
        if int(mask.sum()) < 10:
            return float("nan")
        return float(np.corrcoef(a[mask], b[mask])[0, 1])

    corrs = dict(
        samples=int(ms.sum()),
        full_samples=int(full.sum()),
        r_mstate_tinc=corr(mstate, tinc, ms),
        r_mstate_dthx=corr(mstate, dt_hx, ms),
        r_tb_tinc=corr(tb, tinc, ms),
    )
    full_corr = dict(
        samples=int(full.sum()),
        r_mstate_tinc=corr(mstate, tinc, full),
        r_mstate_dthx=corr(mstate, dt_hx, full),
    )

    return dict(
        regime_counts={k: int((regime == k).sum())
                       for k in ("A", "B", "C", "D", "E", "other")},
        new_active_count=int(new_active.sum()),
        old_active_count=int(old_active.sum()),
        new=new_r, old=old_r,
        ratio_B1=new_r["ratio_B1"], ratio_B2=new_r["ratio_B2"],
        ratio_C=new_r["ratio_C"],
        tau_rad_off=(float(rad_stats["median_s"])
                     if rad_stats["segments"] else _fallback(old_rad_tau)),
        tau_rad_off_stats=rad_stats,
        tau_rad_off_segments=rad_fits,
        tau_rad_off_legacy=old_rad_tau,
        tau_rad_ratio_per_min_legacy=old_rad_ratio,
        tau_rad_samples_legacy=int(sel.sum()),
        tau_idle=(float(idle_stats["median_s"])
                  if idle_stats["segments"] else _fallback(old_idle_tau)),
        tau_idle_stats=idle_stats,
        tau_idle_segments=best_fits,
        tau_idle_legacy=old_idle_tau,
        tau_idle_ratio_per_min_legacy=old_idle_ratio,
        tau_idle_samples_legacy=int(sel_i.sum()),
        room_offset_c=float(best_delta),
        room_offset_adopted=bool(adopt_delta),
        room_offset_boundary=bool(not interior),
        room_offset_range=(delta_lo, delta_hi),
        room_offset_sse=float(best_sse),
        room_offset_sse_adopted=float(best_sse if adopt_delta else sse0),
        room_offset_sse_zero=float(sse0),
        fit_window=FIT_WINDOW,
        fit_regime_a_correlations=corrs,
        fit_full_window_correlations=full_corr,
        fresh_samples=int(fresh.sum()),
        stale_samples=int((~fresh).sum()),
    )


def identify_hx_fit1(con: duckdb.DuckDBPyConnection) -> dict:
    """Fit tau_hx and eps_A on the shortened FIT-1 with input-only masking."""
    df = load_episode(con, EPISODES["FIT-1"]["t_start"],
                      EPISODES["FIT-1"]["t_end"], warmup_min=0.0)
    tb = df["t_boiler"].values
    tinc = df["t_inc"].values
    tret = df["t_ret"].values
    bp = df["burner_pct"].values
    fresh = freshness_mask(df)
    p1 = (df["p1_w"].values >= 2.0).astype(int)
    p2 = (df["p2_w"].values >= 2.0).astype(int)
    p3 = (df["p3_w"].values >= 2.0).astype(int)
    ww = df["ww_flag"].values >= 0.5
    stable = np.ones(len(df), dtype=bool)
    for arr in (p1, p2, p3):
        for k in range(5, len(df)):
            stable[k] = stable[k] and bool(np.all(arr[k - 5:k + 1] == arr[k]))
    mask = fresh & (~ww) & (bp > 5.0) & ((tb - tret) > 10.0) & stable
    assert int(mask.sum()) > 200, "Insufficient fresh FIT-1 samples"

    def loss(p):
        tau, eps = float(p[0]), float(p[1])
        alpha = DT / (tau + DT)
        sim = np.empty(len(tb))
        sim[0] = tinc[0]
        for k in range(1, len(tb)):
            target = tret[k] + eps * (tb[k] - tret[k])
            sim[k] = (1.0 - alpha) * sim[k - 1] + alpha * target
        return float(np.mean((sim[mask] - tinc[mask]) ** 2))

    res = minimize(loss, [70.0, 0.94], bounds=[(10.0, 300.0), (0.80, 0.99)],
                   method="L-BFGS-B")
    tau_hx = float(res.x[0])
    eps_A = float(res.x[1])
    assert 20.0 <= tau_hx <= 200.0 and 0.85 <= eps_A <= 0.99
    return dict(tau_hx=tau_hx, eps_A=eps_A, mse=float(res.fun),
                rmse=float(math.sqrt(res.fun)), samples=int(mask.sum()),
                excluded_stale=int((~fresh).sum()),
                window=(EPISODES["FIT-1"]["t_start"], EPISODES["FIT-1"]["t_end"]))


def identify_dhw_fit(con: duckdb.DuckDBPyConnection) -> dict:
    """Fit dhw_delay_s and tau_dhw_fast on FIT-DHW (10 s, no bfill)."""
    df = load_episode(con, EPISODES["FIT-DHW"]["t_start"], EPISODES["FIT-DHW"]["t_end"], warmup_min=0.0)
    ww = df["ww_flag"].values > 0.5
    edges = np.where(ww[1:] & ~ww[:-1])[0] + 1
    assert len(edges) >= 1, "FIT-DHW has no DHW rising edge"
    e = int(edges[0])
    tinc = df["t_inc"].values
    tret = df["t_ret"].values

    best = None
    for delay in np.arange(0.0, 241.0, 10.0):
        def loss(tau):
            sim = np.empty(len(df))
            sim[: e + 1] = tinc[: e + 1]
            alpha = DT / (tau + DT)
            for k in range(e + 1, len(df)):
                if (k - e) * DT < delay:
                    sim[k] = sim[k - 1]
                else:
                    sim[k] = (1.0 - alpha) * sim[k - 1] + alpha * tret[k]
            return float(np.mean((sim[e : e + 60] - tinc[e : e + 60]) ** 2))

        r = minimize_scalar(loss, bounds=(20.0, 300.0), method="bounded")
        if best is None or r.fun < best[0]:
            best = (float(r.fun), float(delay), float(r.x))
    mse, delay, tau = best
    assert 0.0 <= delay <= 240.0 and 20.0 <= tau <= 300.0
    return dict(dhw_delay_s=delay, tau_dhw_fast=tau, mse=mse,
                rmse=float(math.sqrt(mse)), edge_index=e,
                edge_time=str(df["t"].iloc[e]))


# ---------------------------------------------------------------------------
# Causal majority and the discrete-time simulator
# ---------------------------------------------------------------------------
class CausalMajority:
    """Incremental trailing majority over `window` samples.

    Mirrors the C++ implementation exactly: a fixed-size ring, a running count
    and a running sum, with the integer test 2*ones >= count.
    """

    __slots__ = ("window", "buf", "pos", "count", "ones")

    def __init__(self, window: int = 31):
        self.window = int(window)
        self.buf = [0] * self.window
        self.pos = 0
        self.count = 0
        self.ones = 0

    def update(self, raw: int) -> bool:
        old = self.buf[self.pos]
        self.buf[self.pos] = int(raw)
        self.pos = (self.pos + 1) % self.window
        if self.count < self.window:
            self.count += 1
        self.ones += int(raw) - old
        return 2 * self.ones >= self.count

    def reset(self) -> None:
        for i in range(self.window):
            self.buf[i] = 0
        self.pos = 0
        self.count = 0
        self.ones = 0


# Branch identifiers (kept identical in C++).
BR_ACTIVE = 0
BR_CUTOFF = 1
BR_DHW = 2
BR_IDLE = 3
BR_CASING = 4
BR_POST_DHW = 5
BR_STALE = 6
BR_HOLD = 7

BRANCH_NAMES = {
    BR_ACTIVE: "active_heating",
    BR_CUTOFF: "circ_unheated_cutoff",
    BR_DHW: "dhw_divert",
    BR_IDLE: "idle",
    BR_CASING: "casing_decay",
    BR_POST_DHW: "post_dhw_return",
    BR_STALE: "stale_hold",
    BR_HOLD: "pump_off_hold",
}


class HXSimulator:
    """Discrete-time HX outlet simulator.

    `mode` selects the historical architecture for the report:
      v3: no cutoff branch (v3 baseline)
      v4: immediate clamped cutoff (v4)
      v5: two-stage pump-off hold + burner casing decay (v5)
      v6: decoupled low-lift, cold-start front, staleness guard (this release)

    The v6 code path is mirrored line-for-line by the generated C++ header.
    """

    __slots__ = (
        "p", "mode", "stale_rule", "cm1", "cm2", "cm3", "n_cold", "ring", "rpos",
        "sim", "t_hx", "t_front", "tb_active", "consec_bp",
        "dhw_timer", "post_dhw_timer", "pump_off_timer", "burner_off_timer",
        "idle_timer", "cold_on", "cold_elapsed", "branch_counts",
    )

    def __init__(self, params: dict, mode: str = "v6",
                 stale_rule: str = "v63"):
        self.p = params
        self.mode = mode
        # "v63" is the release rule; "v62" reproduces the observed v6.2
        # staleness for the attribution diagnostic only.
        self.stale_rule = stale_rule
        self.cm1 = CausalMajority(31)
        self.cm2 = CausalMajority(31)
        self.cm3 = CausalMajority(31)
        if mode == "v6":
            self.n_cold = max(1, min(MAX_COLD_RING_N,
                                     int(round(float(params["L_cold_start"]) / DT))))
        else:
            self.n_cold = 1
        self.ring = [0.0] * self.n_cold
        self.rpos = 0
        self.branch_counts = {name: 0 for name in BRANCH_NAMES.values()}
        self.reset(0.0, 0.0)

    def reset(self, t_inc0: float, t_boiler0: float) -> None:
        self.cm1.reset()
        self.cm2.reset()
        self.cm3.reset()
        self.sim = float(t_inc0)
        self.t_hx = float(t_inc0)
        self.t_front = float(t_inc0)
        self.tb_active = float(t_boiler0)
        self.consec_bp = 0
        if self.mode == "v6":
            # Required identical initial timers.
            self.dhw_timer = 0.0
            self.post_dhw_timer = 9999.0
            self.pump_off_timer = 9999.0
            self.burner_off_timer = 9999.0
            self.idle_timer = 9999.0  # "cold_start" timer: assume long idle
            self.cold_on = False
            self.cold_elapsed = 0.0
        else:
            # Frozen v3/v4/v5 behaviour for the historical comparison.
            self.dhw_timer = 0.0
            self.post_dhw_timer = 9999.0
            self.pump_off_timer = 0.0
            self.burner_off_timer = 0.0
            self.idle_timer = 9999.0
            self.cold_on = False
            self.cold_elapsed = 0.0
        for i in range(self.n_cold):
            self.ring[i] = float(t_inc0)
        self.rpos = 0
        for k in self.branch_counts:
            self.branch_counts[k] = 0

    def reanchor(self, t_inc0: float, tb0: float) -> None:
        """Re-anchor the water states at the evaluation boundary.

        The 30-minute warm-up primes the causal pump filters and the event
        timers, but an unobserved pre-window transient must not bias the
        target window.  Only the thermal states are reset to the measured
        manifold/boiler values; filters and timers keep their warm-up history.
        An active cold-start front is *not* wiped, and branch counters are
        cleared so they describe the target window only.
        """
        self.sim = float(t_inc0)
        self.t_hx = float(t_inc0)
        self.tb_active = float(tb0)
        if not self.cold_on:
            self.t_front = float(t_inc0)
            for i in range(self.n_cold):
                self.ring[i] = float(t_inc0)
            self.rpos = 0
        for k in self.branch_counts:
            self.branch_counts[k] = 0

    def step(self, tb: float, tret: float, troom: float,
             bp: float, pmod: float, p1w: float, p2w: float, p3w: float,
             ww: float, bp_age: float, pmod_age: float, tb_age: float,
             tret_age: float, p3_age: float, p1_age: float, p2_age: float):
        p = self.p

        # -- Telemetry validity (v6 only) -----------------------------------
        if self.mode == "v6":
            if self.stale_rule == "v62":
                # Exact v6.2 rule, used only to attribute the observed
                # holdout failure; treat every pump power as analog.
                valid = (
                    (tb_age <= TB_MAX_AGE_S)
                    and (tret_age <= TB_MAX_AGE_S)
                    and (p3_age <= TB_MAX_AGE_S)
                    and (bp_age <= EVT_MAX_AGE_S)
                    and (pmod_age <= EVT_MAX_AGE_S)
                )
            else:
                # v6.3: analog ages gate the heartbeat; event-driven signals
                # (burner, pump modulation, all pump powers) are stale only
                # when their own age is very old *and* the tb heartbeat is
                # stale.
                event_stale = (
                    (bp_age > EVT_MAX_AGE_S)
                    or (pmod_age > EVT_MAX_AGE_S)
                    or (p1_age > EVT_MAX_AGE_S)
                    or (p2_age > EVT_MAX_AGE_S)
                    or (p3_age > EVT_MAX_AGE_S)
                ) and (tb_age > TB_MAX_AGE_S)
                valid = (
                    (tb_age <= TB_MAX_AGE_S)
                    and (tret_age <= TB_MAX_AGE_S)
                    and not event_stale
                )
            if not valid:
                # Hold every state variable.  The plug-flow ring must not
                # advance without valid primary-flow telemetry.
                self.branch_counts["stale_hold"] += 1
                return self.sim, BR_STALE

        p1 = self.cm1.update(1 if p1w >= 2.0 else 0)
        p2 = self.cm2.update(1 if p2w >= 2.0 else 0)
        p3 = self.cm3.update(1 if p3w >= 2.0 else 0)
        pany = p1 or p2 or p3
        rad_cnt = (1 if p1 else 0) + (1 if p2 else 0)
        wwb = ww >= 0.5
        tr = 20.0 if math.isnan(troom) else float(troom)

        # -- Timers ----------------------------------------------------------
        if wwb:
            self.dhw_timer += DT
            self.post_dhw_timer = 0.0
        else:
            self.dhw_timer = 0.0
            self.post_dhw_timer += DT

        if bp > 0.0:
            self.consec_bp += 1
            if self.consec_bp == 1:
                # Single-step firing glitch fix: refresh the casing estimate
                # without clearing the burner-off timer.
                if tb > self.tb_active:
                    self.tb_active = tb
            else:
                self.tb_active = tb
                self.burner_off_timer = 0.0
        else:
            self.consec_bp = 0
            self.burner_off_timer += DT

        if pmod <= 0.0:
            self.pump_off_timer += DT
        else:
            self.pump_off_timer = 0.0

        # -- Cold-start latch ------------------------------------------------
        active_now = (bp > 0.0) or (pmod > 0.0)
        if self.mode == "v6":
            if active_now:
                # HIL-safe trigger: use the simulator's own outlet state, never
                # the measured T_incoming (which is the Step 2 output).
                if ((not self.cold_on) and (self.idle_timer >= L_IDLE_TRIGGER_S)
                        and (self.sim < COLD_EXIT_TEMP_C)
                        and (tb - self.sim > 15.0)):
                    self.cold_on = True
                    self.cold_elapsed = 0.0
                    self.t_front = float(self.sim)
                    for i in range(self.n_cold):
                        self.ring[i] = float(self.sim)
                    self.rpos = 0
                self.idle_timer = 0.0
            else:
                self.idle_timer += DT
                # A boiler/pump abort back to prolonged idle must unlatch the
                # cold-start front; otherwise cold_elapsed stops advancing
                # (pmod <= 0) and the exit condition can never fire.
                if self.cold_on and self.idle_timer >= L_IDLE_TRIGGER_S:
                    self.cold_on = False

            if self.cold_on:
                # A plug-flow pipe advances only while primary water actually
                # flows through the space-heating HX.  During a primary-pump
                # stop or a DHW divert the front is held in place.
                if pmod > 0.0 and not wwb:
                    delayed = self.ring[self.rpos]
                    tau_cold = float(p["tau_cold_front"])
                    a_cold = 1.0 if tau_cold <= 0.0 else DT / (tau_cold + DT)
                    self.t_front = (1.0 - a_cold) * self.t_front + a_cold * delayed
                    self.ring[self.rpos] = float(tb)
                    self.rpos = (self.rpos + 1) % self.n_cold
                    self.cold_elapsed += DT
                tb_use = self.t_front
            else:
                tb_use = float(tb)
        else:
            tb_use = float(tb)

        # -- Effectiveness by secondary regime -------------------------------
        if p3 and not (p1 or p2):
            eps = float(p["eps_A"])
        elif p3 and rad_cnt == 1:
            eps = float(p["eps_B1"])
        elif p3 and rad_cnt == 2:
            eps = float(p["eps_B2"])
        elif (not p3) and rad_cnt > 0:
            eps = float(p["eps_C"])
        else:
            eps = float(p["eps_A"])

        prev = self.sim
        branch = BR_ACTIVE

        # -- Branch logic ----------------------------------------------------
        if wwb and pany:
            branch = BR_DHW
            if self.dhw_timer < p["dhw_delay_s"]:
                out = prev
            else:
                a = DT / (p["tau_dhw_fast"] + DT)
                out = (1.0 - a) * prev + a * tret
        elif pany:
            if self.post_dhw_timer < p["post_dhw_delay_s"]:
                branch = BR_POST_DHW
                a = DT / (p["tau_cutoff"] + DT)
                out = (1.0 - a) * prev + a * tret
            elif self.mode == "v3":
                branch = BR_ACTIVE
                target = tret + eps * (tb - tret)
                a = DT / (p["tau_hx"] + DT)
                out = (1.0 - a) * prev + a * target
            elif self.mode == "v4":
                if (pmod <= 0.0) or (tb <= tret + 1.0):
                    branch = BR_CUTOFF
                    a = DT / (p["tau_cutoff"] + DT)
                    out = (1.0 - a) * prev + a * tret
                elif bp <= 0.0:
                    branch = BR_CASING
                    tb_eff = min(tb, self.tb_active)
                    target = tret + eps * (tb_eff - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
                else:
                    branch = BR_ACTIVE
                    target = tret + eps * (tb - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
            elif self.mode == "v5":
                if pmod <= 0.0:
                    branch = BR_CUTOFF
                    if self.pump_off_timer < p["L_pump_off"]:
                        branch = BR_HOLD
                        out = prev
                    else:
                        a = DT / (p["tau_cutoff"] + DT)
                        out = (1.0 - a) * prev + a * tret
                elif self.burner_off_timer > 0.0:
                    branch = BR_CASING
                    if self.burner_off_timer < p["L_burn_off"]:
                        tb_eff = min(tb, self.tb_active)
                    else:
                        a_decay = DT / (p["tau_burn_off"] + DT)
                        self.tb_active = (1.0 - a_decay) * self.tb_active + a_decay * tret
                        tb_eff = min(tb, self.tb_active)
                    target = tret + eps * (tb_eff - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
                else:
                    branch = BR_ACTIVE
                    target = tret + eps * (tb - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
            else:
                # v6: decoupled low-lift / pump-off / casing / active
                if pmod <= 0.0:
                    if self.pump_off_timer < p["L_pump_off"]:
                        branch = BR_HOLD
                        out = prev
                    elif p3:
                        branch = BR_CUTOFF
                        a = DT / (p["tau_cutoff"] + DT)
                        out = (1.0 - a) * prev + a * tret
                    else:
                        branch = BR_CUTOFF
                        a = DT / (p["tau_rad_off"] + DT)
                        out = (1.0 - a) * prev + a * tret
                elif tb <= tret + 1.0:
                    # Branch 2B: pump ON, no usable lift -> immediate cutoff.
                    branch = BR_CUTOFF
                    a = DT / (p["tau_cutoff"] + DT)
                    out = (1.0 - a) * prev + a * tret
                elif self.burner_off_timer > 0.0:
                    branch = BR_CASING
                    if self.burner_off_timer < p["L_burn_off"]:
                        tb_eff = min(tb_use, self.tb_active)
                    else:
                        a_decay = DT / (p["tau_burn_off"] + DT)
                        self.tb_active = (1.0 - a_decay) * self.tb_active + a_decay * tret
                        tb_eff = min(tb_use, self.tb_active)
                    target = tret + eps * (tb_eff - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
                else:
                    branch = BR_ACTIVE
                    target = tret + eps * (tb_use - tret)
                    a = DT / (p["tau_hx"] + DT)
                    out = (1.0 - a) * prev + a * target
        else:
            branch = BR_IDLE
            tau_idle = p["tau_idle"] if self.mode == "v6" else p["tau_ambient"]
            a = DT / (tau_idle + DT)
            out = (1.0 - a) * prev + a * tr

        self.sim = float(out)
        self.t_hx = float(out)

        # -- Cold-start exit -------------------------------------------------
        if self.mode == "v6" and self.cold_on:
            if (self.sim >= COLD_EXIT_TEMP_C
                    or self.cold_elapsed >= p["L_cold_start"] + COLD_EXIT_TAU_MULT * p["tau_cold_front"]):
                self.cold_on = False
                self.t_front = float(tb)

        self.branch_counts[BRANCH_NAMES[branch]] += 1
        return self.sim, branch


# ---------------------------------------------------------------------------
# Episode execution and metrics
# ---------------------------------------------------------------------------
SIM_INPUT_COLUMNS = [
    "t_boiler", "t_inc", "t_ret", "t_room", "burner_pct", "pump_mod",
    "p1_w", "p2_w", "p3_w", "ww_flag", "bp_age_s", "pmod_age_s", "tb_age_s",
    "tret_age_s", "p3_age_s", "p1_age_s", "p2_age_s",
]


def simulate_episode(df: pd.DataFrame, params: dict, mode: str = "v6",
                     reanchor_index: int = 0,
                     stale_rule: str = "v63") -> dict:
    """Run the simulator over a loaded episode; return metrics and arrays.

    `reanchor_index` is the first evaluation sample (normally `WARMUP_STEPS`).
    At that row the thermal state is re-anchored to the measured values while
    the causal filters and timers retain the warm-up history.  A value of 0
    disables re-anchoring (used for FIT episodes and synthetic unit tests).
    """
    n = len(df)
    tb = df["t_boiler"].values
    tinc = df["t_inc"].values
    tret = df["t_ret"].values
    troom = df["t_room"].values
    bp = df["burner_pct"].values
    pmod = df["pump_mod"].values
    p1 = df["p1_w"].values
    p2 = df["p2_w"].values
    p3 = df["p3_w"].values
    ww = df["ww_flag"].values
    bpa = df["bp_age_s"].values
    pmoda = df["pmod_age_s"].values
    tba = df["tb_age_s"].values
    treta = df["tret_age_s"].values
    p3a = df["p3_age_s"].values
    p1a = df["p1_age_s"].values
    p2a = df["p2_age_s"].values
    tinca = df["tinc_age_s"].values

    sim = np.empty(n)
    branches = np.empty(n, dtype=np.int8)
    s = HXSimulator(params, mode=mode, stale_rule=stale_rule)
    s.reset(tinc[0], tb[0])
    sim[0] = s.sim
    branches[0] = BR_ACTIVE
    for k in range(1, n):
        if k == reanchor_index:
            assert tinca[k] <= TB_MAX_AGE_S, (
                f"Stale re-anchor ground truth at index {k}: "
                f"tinc_age_s={tinca[k]:.0f} s > {TB_MAX_AGE_S:.0f} s"
            )
            # Re-anchor at the boundary, then still process this sample so
            # timers and causal pump filters advance without a 10 s gap.
            s.reanchor(tinc[k], tb[k])
        out, br = s.step(tb[k], tret[k], troom[k], bp[k], pmod[k],
                         p1[k], p2[k], p3[k], ww[k], bpa[k], pmoda[k], tba[k],
                         treta[k], p3a[k], p1a[k], p2a[k])
        sim[k] = out
        branches[k] = br
    return dict(sim=sim, branches=branches, branch_counts=dict(s.branch_counts),
                tinc_true=tinc, tinc_age=tinca)


def window_metrics(sim: np.ndarray, truth: np.ndarray, start: int = 0,
                   stop: int | None = None,
                   tinc_age: np.ndarray | None = None) -> dict:
    if stop is None:
        stop = len(truth)
    sim = np.asarray(sim[start:stop], dtype=float)
    truth = np.asarray(truth[start:stop], dtype=float)
    if tinc_age is None:
        valid = np.ones(len(truth), dtype=bool)
        max_age = float("nan")
    else:
        age = np.asarray(tinc_age[start:stop], dtype=float)
        valid = age <= TB_MAX_AGE_S
        max_age = float(np.max(age)) if len(age) else float("nan")
    excluded = int((~valid).sum())
    sim = sim[valid]
    truth = truth[valid]
    if len(truth) == 0:
        return dict(rmse=float("nan"), mae=float("nan"), p95=float("nan"),
                    bias=float("nan"), maxabs=float("nan"), r2=float("nan"),
                    samples=0, excluded_samples=excluded,
                    tinc_max_age_s=max_age)
    e = sim - truth
    rmse = float(np.sqrt(np.mean(e * e)))
    mae = float(np.mean(np.abs(e)))
    p95 = float(np.percentile(np.abs(e), 95))
    bias = float(np.mean(e))
    maxabs = float(np.max(np.abs(e)))
    ss_tot = float(np.sum((truth - np.mean(truth)) ** 2))
    r2 = float(1.0 - np.sum(e * e) / ss_tot) if ss_tot > 0 else 0.0
    return dict(rmse=rmse, mae=mae, p95=p95, bias=bias, maxabs=maxabs,
                r2=r2, samples=int(len(truth)), excluded_samples=excluded,
                tinc_max_age_s=max_age)


def detect_shutdown_windows(df: pd.DataFrame, start: int = WARMUP_STEPS,
                            cold_steps: int = 30) -> dict:
    """Locate cold-start, genuine mid-run cutoffs and the final shutdown.

    v6.3 requires a burner relight after a mid-run cutoff.  A burner-off phase
    without a later relight is a run-down and is folded into the final-shutdown
    window, including the following pump-off run.
    """
    n = len(df)
    bp = df["burner_pct"].values
    pmod = df["pump_mod"].values
    tb = df["t_boiler"].values
    tret = df["t_ret"].values
    tinc = df["t_inc"].values
    ww = df["ww_flag"].values >= 0.5
    p1 = causal_majority_np(df["p1_w"].values >= 2.0, window=31)
    p2 = causal_majority_np(df["p2_w"].values >= 2.0, window=31)
    p3 = causal_majority_np(df["p3_w"].values >= 2.0, window=31)
    pany = p1 | p2 | p3
    sh_circ = pany & (~ww)

    # Final pump shutdown: last primary-pump-off run with space-heating
    # circulation still active, at least 3 minutes long.
    final_pump = None
    i = start
    while i < n:
        if pmod[i] <= 0.0 and sh_circ[i]:
            j = i
            while j + 1 < n and pmod[j + 1] <= 0.0 and sh_circ[j + 1]:
                j += 1
            if j - i + 1 >= 18:
                final_pump = (i, j + 1)
            i = j + 1
        else:
            i += 1

    # Burner-off phases with primary circulation and usable lift.
    phases = []
    i = start
    while i < n:
        cond = (bp[i] <= 0.0) and (pmod[i] > 0.0) and sh_circ[i] \
            and (tb[i] > tret[i] + 1.0)
        if cond:
            j = i
            while (j + 1 < n and bp[j + 1] <= 0.0 and pmod[j + 1] > 0.0
                   and sh_circ[j + 1] and tb[j + 1] > tret[j + 1] + 1.0):
                j += 1
            phases.append((i, j + 1))
            i = j + 1
        else:
            i += 1

    cutoffs = []
    rundowns = []
    for a, b in phases:
        relight = bool(np.any(bp[b:] > 0.0))
        if relight and (b - a) >= 12:
            cutoffs.append((a, b))
        elif not relight:
            rundowns.append((a, b))

    final = final_pump
    if rundowns:
        a = min(r[0] for r in rundowns)
        if final is None:
            final = (a, n)
        else:
            final = (min(a, final[0]), final[1])

    # Cold-start window: from the first target sample with space-heating
    # circulation until the measured transient length, not the 28 C crossing.
    cold = None
    for k in range(start, min(n, start + 30)):
        if sh_circ[k] and tinc[k] < COLD_EXIT_TEMP_C:
            cold = (k, min(n, k + max(30, int(cold_steps))))
            break

    return dict(cold=cold, cutoffs=cutoffs, final=final,
                rundowns=rundowns)


def union_windows(windows: list[tuple[int, int]]) -> np.ndarray:
    mask = np.zeros(0, dtype=bool)
    if not windows:
        return mask
    n = max(w[1] for w in windows)
    mask = np.zeros(n, dtype=bool)
    for a, b in windows:
        mask[a:b] = True
    return mask


def metrics_on_windows(sim: np.ndarray, truth: np.ndarray,
                       windows: list[tuple[int, int]],
                       tinc_age: np.ndarray | None = None) -> dict:
    if not windows:
        return dict(rmse=float("nan"), mae=float("nan"), bias=float("nan"),
                    maxabs=float("nan"), samples=0, excluded_samples=0,
                    tinc_max_age_s=float("nan"))
    idx = np.concatenate([np.arange(a, b) for a, b in windows])
    sim_w = np.asarray(sim, dtype=float)[idx]
    truth_w = np.asarray(truth, dtype=float)[idx]
    if tinc_age is None:
        valid = np.ones(len(truth_w), dtype=bool)
        max_age = float("nan")
    else:
        age = np.asarray(tinc_age, dtype=float)[idx]
        valid = age <= TB_MAX_AGE_S
        max_age = float(np.max(age)) if len(age) else float("nan")
    excluded = int((~valid).sum())
    sim_w = sim_w[valid]
    truth_w = truth_w[valid]
    if len(truth_w) == 0:
        return dict(rmse=float("nan"), mae=float("nan"), bias=float("nan"),
                    maxabs=float("nan"), samples=0, excluded_samples=excluded,
                    tinc_max_age_s=max_age)
    e = sim_w - truth_w
    return dict(rmse=float(np.sqrt(np.mean(e * e))), mae=float(np.mean(np.abs(e))),
                bias=float(np.mean(e)), maxabs=float(np.max(np.abs(e))),
                samples=int(len(e)), excluded_samples=excluded,
                tinc_max_age_s=max_age)


# ---------------------------------------------------------------------------
# TUNE stage: dedicated cold-start fit + coordinate grid search
# ---------------------------------------------------------------------------
COLD_FIT_STEPS = 30  # updated in main() from the measured TUNE transient


def base_params_from_fit(fit_ratios: dict, hx_fit: dict, dhw_fit: dict) -> dict:
    eps_A = float(hx_fit["eps_A"])
    return {
        "tau_hx": float(hx_fit["tau_hx"]),
        "eps_A": eps_A,
        "eps_B1": min(0.98, eps_A * float(fit_ratios["ratio_B1"])),
        "eps_B2": min(0.98, eps_A * float(fit_ratios["ratio_B2"])),
        "eps_C": min(0.98, eps_A * float(fit_ratios["ratio_C"])),
        "dhw_delay_s": float(dhw_fit["dhw_delay_s"]),
        "tau_dhw_fast": float(dhw_fit["tau_dhw_fast"]),
        "post_dhw_delay_s": 90.0,
        "L_pump_off": 70.0,
        "L_burn_off": 30.0,
        "tau_burn_off": 2400.0,
        "tau_cutoff": 150.0,
        "tau_idle": float(fit_ratios["tau_idle"]),
        "tau_rad_off": float(fit_ratios["tau_rad_off"]),
        "L_cold_start": 140.0,
        "tau_cold_front": 10.0,
    }


def measure_cold_transient_steps(con: duckdb.DuckDBPyConnection,
                                 reference_params: dict) -> tuple[int, list]:
    """Phase 1.7 transient length, computed on TUNE cold starts only.

    The duration is the number of consecutive samples with |sim-truth| > 1 C
    after the cold-start window begins, maximised over the frozen v5 constants
    and the v6.2 reference constants.  It is used to size the cold-start fit
    and report windows.
    """
    durations = []
    for eid, meta in EPISODES.items():
        if meta["kind"] != "TUNE":
            continue
        df = load_episode(con, meta["t_start"], meta["t_end"])
        wins = detect_shutdown_windows(df, cold_steps=30)
        if not wins["cold"]:
            continue
        start = wins["cold"][0]
        for label, p, mode in (("v5", dict(V5_FROZEN), "v5"),
                               ("v6.2", dict(reference_params), "v6")):
            res = simulate_episode(df, p, mode=mode, reanchor_index=WARMUP_STEPS)
            e = np.abs(res["sim"] - res["tinc_true"])
            k = start
            while k < len(df) and e[k] > 1.0:
                k += 1
            durations.append(dict(episode=eid, model=label,
                                  duration_s=float((k - start) * DT),
                                  cold_start=int(start)))
    steps = max(30, int(math.ceil(max([d["duration_s"] for d in durations],
                                     default=0.0) / DT)))
    steps = int(min(steps, MAX_COLD_RING_N))
    return steps, durations


def fit_cold_start_tune(con: duckdb.DuckDBPyConnection, params: dict,
                        cold_steps: int) -> dict:
    """Grid-search L_cold_start and tau_cold_front on **all** TUNE cold starts."""
    episodes = []
    windows = []
    for eid, meta in EPISODES.items():
        if meta["kind"] != "TUNE":
            continue
        df = load_episode(con, meta["t_start"], meta["t_end"])
        wins = detect_shutdown_windows(df, cold_steps=cold_steps)
        if wins["cold"]:
            episodes.append((eid, df))
            windows.append(wins["cold"])
    assert episodes, "No TUNE cold starts found"

    def cold_rmse(trial: dict) -> float:
        sse, count = 0.0, 0
        for (eid, df), (a, b) in zip(episodes, windows):
            res = simulate_episode(df, trial, mode="v6",
                                   reanchor_index=WARMUP_STEPS)
            m = window_metrics(res["sim"], res["tinc_true"], a, b,
                               tinc_age=res["tinc_age"])
            if m["samples"]:
                sse += m["rmse"] ** 2 * m["samples"]
                count += m["samples"]
        return float(math.sqrt(sse / max(count, 1)))

    best = None
    for L in [0.0 + 10.0 * i for i in range(33)]:  # 0..320 s
        assert L / DT <= MAX_COLD_RING_N
        for tau in [10.0, 20.0, 30.0, 50.0, 80.0, 120.0]:
            trial = dict(params)
            trial["L_cold_start"] = L
            trial["tau_cold_front"] = tau
            rmse = cold_rmse(trial)
            if best is None or rmse < best["rmse"] - 1e-12:
                best = dict(rmse=rmse, L_cold_start=L, tau_cold_front=tau)
    assert best is not None and best["tau_cold_front"] >= DT
    return best


def tune_v6(con: duckdb.DuckDBPyConnection, params0: dict,
            cold_steps: int) -> dict:
    """Coordinate grid search over the complete TUNE partition.

    v6.3 grids include zero floors (0 disables a hold).  A parameter that ends
    on a grid boundary is extended and re-searched; if it still ends on the
    boundary it is reported as boundary-limited.  tau_cold_front is constrained
    to >= DT, so it can no longer degenerate into a pure delay.
    """
    tune = {
        eid: load_episode(con, meta["t_start"], meta["t_end"])
        for eid, meta in EPISODES.items() if meta["kind"] == "TUNE"
    }

    def score(p):
        rmses = []
        for eid, df in tune.items():
            res = simulate_episode(df, p, mode="v6",
                                   reanchor_index=WARMUP_STEPS)
            m = window_metrics(res["sim"], res["tinc_true"], WARMUP_STEPS,
                               tinc_age=res["tinc_age"])
            rmses.append(m["rmse"])
        return sum(rmses) / len(rmses), rmses

    grid = {
        "L_pump_off": [0.0, 10.0, 20.0, 30.0, 45.0, 60.0, 75.0, 90.0,
                       105.0, 120.0, 150.0, 180.0],
        "L_burn_off": [0.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0],
        "tau_burn_off": [1200.0, 1600.0, 2000.0, 2400.0, 2800.0, 3200.0,
                         4000.0, 4800.0, 5600.0],
        "tau_cutoff": [60.0, 90.0, 120.0, 150.0, 180.0, 240.0, 300.0, 360.0],
        "L_cold_start": [0.0 + 10.0 * i for i in range(33)],
        "tau_cold_front": [10.0, 20.0, 30.0, 50.0, 80.0, 120.0, 160.0],
        "post_dhw_delay_s": [0.0, 30.0, 60.0, 90.0, 120.0, 150.0, 180.0,
                             210.0],
        "dhw_delay_s": [60.0, 75.0, 90.0, 105.0, 120.0, 135.0, 150.0,
                        165.0, 180.0],
        "tau_dhw_fast": [50.0, 60.0, 75.0, 90.0, 110.0, 130.0, 150.0,
                         170.0, 190.0],
    }
    step = {k: (v[1] - v[0]) for k, v in grid.items()}

    params = dict(params0)
    history = []
    best_score, best_pair = score(params)
    history.append(dict(pass_no=0, parameter="initial", value=None,
                        objective=best_score, tune_rmse=best_pair))
    boundary_limited = {}

    for pass_no in range(1, 4):
        for name, candidates in grid.items():
            for _attempt in range(3):
                local_best = (best_score, params[name], best_pair)
                for cand in candidates:
                    if abs(cand - params[name]) < 1e-12:
                        continue
                    trial = dict(params)
                    trial[name] = cand
                    sc, pair = score(trial)
                    if sc < local_best[0] - 1e-12:
                        local_best = (sc, cand, pair)
                if local_best[1] != params[name]:
                    params[name] = local_best[1]
                    best_score, best_pair = local_best[0], local_best[2]
                    history.append(dict(pass_no=pass_no, parameter=name,
                                        value=local_best[1],
                                        objective=best_score,
                                        tune_rmse=best_pair))
                # Extend the grid when the selected value sits on a boundary.
                if params[name] in (candidates[0], candidates[-1]):
                    if params[name] == candidates[-1]:
                        extra = [candidates[-1] + step[name] * (i + 1)
                                 for i in range(2)]
                        if name == "L_cold_start":
                            extra = [v for v in extra
                                     if v / DT <= MAX_COLD_RING_N]
                        candidates = candidates + extra
                    else:
                        extra = [max(0.0, candidates[0] - step[name] * (i + 1))
                                 for i in range(2)]
                        candidates = extra + candidates
                    grid[name] = candidates
                    continue
                break
            boundary_limited[name] = bool(
                params[name] in (grid[name][0], grid[name][-1]))
            if name == "L_cold_start":
                assert params[name] / DT <= MAX_COLD_RING_N
            if name == "tau_cold_front":
                assert params[name] >= DT

    final_score, final_pair = score(params)
    return dict(params=params, initial=dict(params0), history=history,
                objective=final_score, tune_rmse=final_pair,
                tune_ids=list(tune.keys()), boundary_limited=boundary_limited,
                grids=grid, cold_fit_steps=cold_steps)


# ---------------------------------------------------------------------------
# VAL stage
# ---------------------------------------------------------------------------
def centered_pump_frame(df: pd.DataFrame, window: int = 31) -> pd.DataFrame:
    """Offline centred-median pump comparison frame (diagnostic only)."""
    d = df.copy()
    for c in ("p1_w", "p2_w", "p3_w"):
        on = median_filter((d[c].values >= 2.0).astype(int), window).astype(bool)
        d[c] = np.where(on, 100.0, 0.0)
    return d


def evaluate_suite(con: duckdb.DuckDBPyConnection, params: dict) -> list[dict]:
    records = []
    for eid, meta in EPISODES.items():
        if meta["kind"] != "VAL":
            continue
        df = load_episode(con, meta["t_start"], meta["t_end"])
        res = simulate_episode(df, params, mode="v6",
                               reanchor_index=WARMUP_STEPS)
        m = window_metrics(res["sim"], res["tinc_true"], WARMUP_STEPS,
                           tinc_age=res["tinc_age"])
        dfc = centered_pump_frame(df)
        resc = simulate_episode(dfc, params, mode="v6",
                                reanchor_index=WARMUP_STEPS)
        mc = window_metrics(resc["sim"], resc["tinc_true"], WARMUP_STEPS,
                            tinc_age=resc["tinc_age"])
        wins = detect_shutdown_windows(df)
        records.append(dict(
            id=eid, name=meta["name"], type=meta["role"],
            t_start=meta["t_start"], t_end=meta["t_end"],
            samples=m["samples"],
            excluded_samples=m["excluded_samples"],
            tinc_max_age_s=m["tinc_max_age_s"],
            rmse=m["rmse"], mae=m["mae"], p95=m["p95"], bias=m["bias"],
            maxabs=m["maxabs"], r2=m["r2"],
            offline_centered_filter_rmse_c=mc["rmse"],
            branch_counts=res["branch_counts"],
            windows=dict(
                cold=wins["cold"],
                cutoffs=wins["cutoffs"],
                final=wins["final"],
            ),
            sim=res["sim"], tinc_true=res["tinc_true"], tinc_age=res["tinc_age"],
            df=df,
        ))
    return records


# ---------------------------------------------------------------------------
# Five-way architecture / parameter comparison on TUNE-1
# ---------------------------------------------------------------------------
FIVE_WAY_LABELS = (
    "v3 baseline (frozen v5 constants)",
    "v4 clamped (frozen v5 constants)",
    "v5 two-stage (frozen v5 constants)",
    "v6.3 architecture (frozen v5 constants + fitted cold front)",
    "v6.3 final (fitted v6.3 constants)",
)


def five_way(con: duckdb.DuckDBPyConnection, eid: str,
             v6_params: dict) -> dict:
    """Architecture / parameter comparison for one TUNE episode."""
    df = load_episode(con, EPISODES[eid]["t_start"], EPISODES[eid]["t_end"])
    wins = detect_shutdown_windows(df, cold_steps=COLD_FIT_STEPS)
    cold_windows = [wins["cold"]] if wins["cold"] else []
    cutoff_windows = wins["cutoffs"]
    final_windows = [wins["final"]] if wins["final"] else []

    # The architecture row keeps every shared v5 constant, including the v5
    # idle time constant, and enables only the v6 branch structure plus the
    # cold-start front fitted on TUNE (no literal parameter values).
    arch_params = dict(V5_FROZEN)
    arch_params["tau_idle"] = V5_FROZEN["tau_ambient"]
    arch_params["L_cold_start"] = float(v6_params["L_cold_start"])
    arch_params["tau_cold_front"] = float(v6_params["tau_cold_front"])

    configs = [
        (FIVE_WAY_LABELS[0], "v3", V5_FROZEN),
        (FIVE_WAY_LABELS[1], "v4", V5_FROZEN),
        (FIVE_WAY_LABELS[2], "v5", V5_FROZEN),
        (FIVE_WAY_LABELS[3], "v6", arch_params),
        (FIVE_WAY_LABELS[4], "v6", v6_params),
    ]
    out = {}
    for label, mode, p in configs:
        res = simulate_episode(df, p, mode=mode, reanchor_index=WARMUP_STEPS)
        overall = window_metrics(res["sim"], res["tinc_true"], WARMUP_STEPS,
                                 tinc_age=res["tinc_age"])
        cold = metrics_on_windows(res["sim"], res["tinc_true"], cold_windows,
                                  tinc_age=res["tinc_age"])
        cut = metrics_on_windows(res["sim"], res["tinc_true"], cutoff_windows,
                                 tinc_age=res["tinc_age"])
        fin = metrics_on_windows(res["sim"], res["tinc_true"], final_windows,
                                 tinc_age=res["tinc_age"])
        out[label] = dict(
            mode=mode, overall=overall, cold=cold, cutoffs=cut, final=fin,
            branch_counts=res["branch_counts"], sim=res["sim"],
        )
    return dict(eid=eid, results=out, windows=wins, df=df)


# ---------------------------------------------------------------------------
# Five named synthetic unit tests
# ---------------------------------------------------------------------------
def _synth_frame(n: int, tb, tinc, tret, troom, bp, pmod,
                 p1=0.0, p2=0.0, p3=0.0, ww=0.0) -> pd.DataFrame:
    def arr(x):
        if np.isscalar(x):
            return np.full(n, float(x))
        a = np.asarray(x, dtype=float)
        assert len(a) == n
        return a

    return pd.DataFrame({
        "t_boiler": arr(tb), "t_inc": arr(tinc), "t_ret": arr(tret),
        "t_room": arr(troom), "burner_pct": arr(bp), "pump_mod": arr(pmod),
        "p1_w": arr(p1), "p2_w": arr(p2), "p3_w": arr(p3), "ww_flag": arr(ww),
        "bp_age_s": np.zeros(n), "pmod_age_s": np.zeros(n), "tb_age_s": np.zeros(n),
        "tret_age_s": np.zeros(n), "p3_age_s": np.zeros(n),
        "p1_age_s": np.zeros(n), "p2_age_s": np.zeros(n),
        "tinc_age_s": np.zeros(n),
    })


def build_unit_frames() -> dict[str, pd.DataFrame]:
    """Build the seven synthetic frames once; reused by Python and C++."""
    frames = {}

    # UT1: low-lift settles to T_return = 25.03 C with no pump-off hold.
    frames["UT1_LowLift"] = _synth_frame(
        900, tb=25.5, tinc=60.0, tret=25.03, troom=20.0,
        bp=0.0, pmod=60.0, p3=10.0)

    # UT2: pump-off hold then plunge.
    n = 400
    frames["UT2_PumpOffHoldPlunge"] = _synth_frame(
        n, tb=65.0, tinc=60.0, tret=20.0, troom=20.0,
        bp=np.r_[np.full(10, 60.0), np.zeros(n - 10)],
        pmod=np.r_[np.full(10, 60.0), np.zeros(n - 10)], p3=10.0)

    # UT3: burner-off with pump on keeps casing heat (no immediate plunge).
    n = 400
    frames["UT3_BurnerOffPumpOn"] = _synth_frame(
        n, tb=70.0, tinc=68.0, tret=20.0, troom=20.0,
        bp=np.r_[np.full(60, 60.0), np.zeros(n - 60)], pmod=60.0, p3=10.0)

    # UT4: DHW entry hold + plunge, then post-DHW recovery.
    n = 500
    frames["UT4_DHWEntryReturn"] = _synth_frame(
        n, tb=70.0, tinc=68.0, tret=25.0, troom=20.0, bp=60.0,
        pmod=np.r_[np.full(50, 60.0), np.full(250, 100.0), np.full(200, 60.0)],
        p3=10.0, ww=np.r_[np.zeros(50), np.ones(250), np.zeros(200)])

    # UT5: cold-start dead time then warm-up.
    n = 400
    frames["UT5_ColdStartWarmup"] = _synth_frame(
        n, tb=55.0, tinc=20.0, tret=20.0, troom=20.0,
        bp=np.r_[np.zeros(200), np.full(200, 50.0)],
        pmod=np.r_[np.zeros(200), np.full(200, 60.0)],
        p3=np.r_[np.zeros(200), np.full(200, 10.0)])

    # UT6: single-step firing glitch versus a genuine relight.
    #
    # All three frames share a 100-sample burner-on phase followed by 300
    # burner-off samples.  The glitch frame adds one isolated burner sample at
    # index 200 (consec_bp == 1); the relight frame adds two consecutive
    # burner samples (consec_bp reaches 2).  A no-glitch reference isolates
    # the effect of the single sample.
    n = 400

    def ut6_frame(blip_len: int) -> pd.DataFrame:
        tb6 = np.full(n, 40.0)
        tb6[:100] = 55.0
        bp6 = np.zeros(n)
        bp6[:100] = 60.0
        if blip_len:
            tb6[200:200 + blip_len] = 70.0
            bp6[200:200 + blip_len] = 60.0
        return _synth_frame(n, tb=tb6, tinc=50.0, tret=20.0, troom=20.0,
                            bp=bp6, pmod=60.0, p3=10.0)

    frames["UT6_SingleStepGlitch"] = ut6_frame(1)
    frames["UT6_NoGlitchReference"] = ut6_frame(0)
    frames["UT6_TwoStepRelight"] = ut6_frame(2)

    # UT7: Branch 2B low lift.  Primary pump ON but T_boiler <= T_return+1.
    n = 600
    frames["UT7_LowLiftBranch2B"] = _synth_frame(
        n, tb=25.5, tinc=40.0, tret=25.0, troom=20.0,
        bp=60.0, pmod=60.0, p3=10.0)
    return frames


def run_unit_tests(params: dict, frames: dict[str, pd.DataFrame] | None = None) -> list[dict]:
    if frames is None:
        frames = build_unit_frames()
    tests = []

    res = simulate_episode(frames["UT1_LowLift"], params, mode="v6")
    final = float(res["sim"][-1])
    ok = abs(final - 25.03) < 0.05
    tests.append(dict(name="UT1_LowLift", passed=bool(ok), value=final,
                      expected="~25.03 C", detail=f"final={final:.4f} C"))

    res = simulate_episode(frames["UT2_PumpOffHoldPlunge"], params, mode="v6")
    n_hold = int(round(float(params["L_pump_off"]) / DT))
    hold_slice = res["sim"][10: 10 + max(n_hold, 1)]
    hold_delta = float(np.max(hold_slice) - np.min(hold_slice))
    plunge = float(res["sim"][-1])
    hold_count = int(res["branch_counts"]["pump_off_hold"])
    if n_hold == 0:
        ok = (hold_count == 0 and plunge < 30.0)
        detail = f"L_pump_off=0 -> no hold, final={plunge:.2f} C"
    else:
        ok = (hold_delta < 0.05 and plunge < 30.0 and hold_count >= n_hold)
        detail = (f"hold_delta={hold_delta:.4f} C, hold={hold_count} "
                  f"(expected {n_hold}), final={plunge:.2f} C")
    tests.append(dict(name="UT2_PumpOffHoldPlunge", passed=bool(ok),
                      value=plunge, expected="hold then <30 C", detail=detail))

    res = simulate_episode(frames["UT3_BurnerOffPumpOn"], params, mode="v6")
    final = float(res["sim"][-1])
    ok = 25.0 < final < 50.0 and res["branch_counts"]["casing_decay"] > 250
    tests.append(dict(name="UT3_BurnerOffPumpOn", passed=bool(ok), value=final,
                      expected="25-50 C (casing decay)",
                      detail=f"final={final:.2f} C"))

    res = simulate_episode(frames["UT4_DHWEntryReturn"], params, mode="v6")
    dhw_end = float(res["sim"][299])
    rec_end = float(res["sim"][-1])
    ok = dhw_end < 32.0 and rec_end > 55.0 and res["branch_counts"]["dhw_divert"] > 200
    tests.append(dict(name="UT4_DHWEntryReturn", passed=bool(ok),
                      value=(dhw_end, rec_end),
                      expected="DHW <32 C, recovery >55 C",
                      detail=f"dhw_end={dhw_end:.2f} C, rec_end={rec_end:.2f} C"))

    res = simulate_episode(frames["UT5_ColdStartWarmup"], params, mode="v6")
    flat = float(np.max(res["sim"][200:210]))
    warm = float(res["sim"][-1])
    ok = flat < 21.0 and warm > 45.0
    tests.append(dict(name="UT5_ColdStartWarmup", passed=bool(ok),
                      value=(flat, warm), expected="flat <21 C then >45 C",
                      detail=f"flat={flat:.2f} C, warm={warm:.2f} C"))

    # UT6: consec_bp == 1 must refresh tb_active with max() but must NOT clear
    # burner_off_timer, so the sample stays in the casing branch.  A second
    # consecutive burner sample (consec_bp == 2) must hard-sync the casing
    # temperature and clear the timer, i.e. become active heating.
    br_of = {v: k for k, v in BRANCH_NAMES.items()}
    res_a = simulate_episode(frames["UT6_SingleStepGlitch"], params, mode="v6")
    res_0 = simulate_episode(frames["UT6_NoGlitchReference"], params, mode="v6")
    res_b = simulate_episode(frames["UT6_TwoStepRelight"], params, mode="v6")
    act_a = int(res_a["branch_counts"]["active_heating"])
    act_0 = int(res_0["branch_counts"]["active_heating"])
    act_b = int(res_b["branch_counts"]["active_heating"])
    branch_glitch = int(res_a["branches"][200])
    branch_relight = int(res_b["branches"][201])
    after = float(res_a["sim"][202])
    ok6 = (branch_glitch == br_of["casing_decay"]
           and branch_relight == br_of["active_heating"]
           and act_a == act_0
           and act_b > act_a
           and after < 60.0)
    tests.append(dict(
        name="UT6_SingleStepGlitch", passed=bool(ok6), value=after,
        expected="1-sample blip stays casing; 2-sample relight is active",
        detail=(f"glitch branch={BRANCH_NAMES[branch_glitch]}, "
                f"relight branch={BRANCH_NAMES[branch_relight]}, "
                f"active no-glitch={act_0}/glitch={act_a}/relight={act_b}, "
                f"sim[202]={after:.2f} C")))

    # UT7: Branch 2B (pump ON, no usable lift) is a cutoff, not active heating.
    res7 = simulate_episode(frames["UT7_LowLiftBranch2B"], params, mode="v6")
    res7v5 = simulate_episode(frames["UT7_LowLiftBranch2B"], params, mode="v5")
    b2b = int(res7["branch_counts"]["circ_unheated_cutoff"])
    v5_active = int(res7v5["branch_counts"]["active_heating"])
    final7 = float(res7["sim"][-1])
    ok7 = b2b > 100 and final7 <= 25.1 and v5_active > 100
    tests.append(dict(name="UT7_LowLiftBranch2B", passed=bool(ok7),
                      value=(final7, b2b, v5_active),
                      expected="v6 cutoff branch; v5 active (must fail)",
                      detail=(f"v6 cutoff={b2b}, final={final7:.2f} C; "
                              f"v5 active={v5_active}")))

    return tests


# ---------------------------------------------------------------------------
# C++ code generation, compilation and parity
# ---------------------------------------------------------------------------
HPP_TEMPLATE = r'''// Auto-generated by simulation/step2_hx_dynamics_v63.py
// Phase 2, Version 6.3 -- exact fitted constants.
// Do not edit by hand: re-run the Python identification to regenerate.
#pragma once

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstddef>

#ifndef STEP2_HX_SCALAR
#define STEP2_HX_SCALAR double
#endif

namespace step2_v63 {

using scalar = STEP2_HX_SCALAR;

static constexpr scalar DT = 10.0;
static constexpr scalar TB_MAX_AGE_S = 900.0;
static constexpr scalar EVT_MAX_AGE_S = 14400.0;
static constexpr scalar L_IDLE_TRIGGER_S = 1800.0;
static constexpr scalar COLD_EXIT_TEMP_C = 28.0;
static constexpr scalar COLD_EXIT_TAU_MULT = 5.0;
static constexpr int MAX_COLD_RING_N = 32;

enum Branch {
    BR_ACTIVE = 0,
    BR_CUTOFF = 1,
    BR_DHW = 2,
    BR_IDLE = 3,
    BR_CASING = 4,
    BR_POST_DHW = 5,
    BR_STALE = 6,
    BR_HOLD = 7
};

struct Params {
    scalar tau_hx = @@TAU_HX@@;
    scalar eps_A = @@EPS_A@@;
    scalar eps_B1 = @@EPS_B1@@;
    scalar eps_B2 = @@EPS_B2@@;
    scalar eps_C = @@EPS_C@@;
    scalar dhw_delay_s = @@DHW_DELAY@@;
    scalar tau_dhw_fast = @@TAU_DHW@@;
    scalar post_dhw_delay_s = @@POST_DHW@@;
    scalar L_pump_off = @@L_PUMP_OFF@@;
    scalar L_burn_off = @@L_BURN_OFF@@;
    scalar tau_burn_off = @@TAU_BURN_OFF@@;
    scalar tau_cutoff = @@TAU_CUTOFF@@;
    scalar tau_idle = @@TAU_IDLE@@;
    scalar tau_rad_off = @@TAU_RAD_OFF@@;
    scalar L_cold_start = @@L_COLD_START@@;
    scalar tau_cold_front = @@TAU_COLD_FRONT@@;
};

template <int MAX_WINDOW = 32>
class CausalMajority {
public:
    explicit CausalMajority(int window = 31)
        : window_(window), pos_(0), count_(0), ones_(0) {
        assert(window_ > 0 && window_ <= MAX_WINDOW);
        for (int i = 0; i < window_; ++i) buf_[i] = 0;
    }

    bool update(int raw) {
        int old = buf_[pos_];
        buf_[pos_] = raw;
        pos_ = (pos_ + 1) % window_;
        if (count_ < window_) ++count_;
        ones_ += raw - old;
        return 2 * ones_ >= count_;
    }

    void reset() {
        for (int i = 0; i < window_; ++i) buf_[i] = 0;
        pos_ = 0;
        count_ = 0;
        ones_ = 0;
    }

private:
    int window_;
    int buf_[MAX_WINDOW];
    int pos_;
    int count_;
    int ones_;
};

class Simulator {
public:
    Simulator() : p_() { init(); }
    explicit Simulator(const Params& p) : p_(p) { init(); }

    const Params& params() const { return p_; }
    scalar sim() const { return sim_; }
    int last_branch() const { return last_branch_; }
    int branch_count(int b) const { return branch_counts_[b]; }

    void reset(scalar t_inc0, scalar tb0) {
        cm1_.reset();
        cm2_.reset();
        cm3_.reset();
        sim_ = t_inc0;
        t_hx_ = t_inc0;
        t_front_ = t_inc0;
        tb_active_ = tb0;
        consec_bp_ = 0;
        dhw_timer_ = scalar(0.0);
        post_dhw_timer_ = scalar(9999.0);
        pump_off_timer_ = scalar(9999.0);
        burner_off_timer_ = scalar(9999.0);
        idle_timer_ = scalar(9999.0);
        cold_on_ = false;
        cold_elapsed_ = scalar(0.0);
        for (int i = 0; i < n_cold_; ++i) ring_[i] = t_inc0;
        rpos_ = 0;
        for (int i = 0; i < 8; ++i) branch_counts_[i] = 0;
        last_branch_ = BR_ACTIVE;
    }

    // Re-anchor the water states at the evaluation boundary.  Causal filters
    // and timers keep their warm-up history; only thermal states are reset.
    void reanchor(scalar t_inc0, scalar tb0) {
        sim_ = t_inc0;
        t_hx_ = t_inc0;
        tb_active_ = tb0;
        if (!cold_on_) {
            t_front_ = t_inc0;
            for (int i = 0; i < n_cold_; ++i) ring_[i] = t_inc0;
            rpos_ = 0;
        }
        for (int i = 0; i < 8; ++i) branch_counts_[i] = 0;
    }

    scalar step(scalar tb, scalar tret, scalar troom,
                scalar bp, scalar pmod, scalar p1w, scalar p2w, scalar p3w,
                scalar ww, scalar bp_age, scalar pmod_age, scalar tb_age,
                scalar tret_age, scalar p3_age, scalar p1_age,
                scalar p2_age) {
        // v6.3 event-style staleness: analog ages gate the heartbeat; event
        // ages are stale only when very old *and* the tb heartbeat is stale.
        bool event_stale = ((bp_age > EVT_MAX_AGE_S)
                            || (pmod_age > EVT_MAX_AGE_S)
                            || (p1_age > EVT_MAX_AGE_S)
                            || (p2_age > EVT_MAX_AGE_S)
                            || (p3_age > EVT_MAX_AGE_S))
                           && (tb_age > TB_MAX_AGE_S);
        if (!(tb_age <= TB_MAX_AGE_S && tret_age <= TB_MAX_AGE_S
              && !event_stale)) {
            // Hold all state; do not advance the plug-flow ring without
            // valid primary-flow telemetry.
            branch_counts_[BR_STALE]++;
            last_branch_ = BR_STALE;
            return sim_;
        }

        bool p1 = cm1_.update(p1w >= scalar(2.0) ? 1 : 0);
        bool p2 = cm2_.update(p2w >= scalar(2.0) ? 1 : 0);
        bool p3 = cm3_.update(p3w >= scalar(2.0) ? 1 : 0);
        bool pany = p1 || p2 || p3;
        int rad_cnt = (p1 ? 1 : 0) + (p2 ? 1 : 0);
        bool wwb = ww >= scalar(0.5);
        scalar tr = std::isnan(troom) ? scalar(20.0) : troom;

        if (wwb) {
            dhw_timer_ += DT;
            post_dhw_timer_ = scalar(0.0);
        } else {
            dhw_timer_ = scalar(0.0);
            post_dhw_timer_ += DT;
        }

        if (bp > scalar(0.0)) {
            consec_bp_ += 1;
            if (consec_bp_ == 1) {
                // Single-step firing glitch: refresh the casing estimate
                // without clearing the burner-off timer.
                if (tb > tb_active_) tb_active_ = tb;
            } else {
                tb_active_ = tb;
                burner_off_timer_ = scalar(0.0);
            }
        } else {
            consec_bp_ = 0;
            burner_off_timer_ += DT;
        }

        if (pmod <= scalar(0.0)) pump_off_timer_ += DT;
        else pump_off_timer_ = scalar(0.0);

        bool active_now = (bp > scalar(0.0)) || (pmod > scalar(0.0));
        if (active_now) {
            // HIL-safe trigger: use the simulator's own outlet state, never
            // the measured T_incoming (which is the Step 2 output).
            if ((!cold_on_) && (idle_timer_ >= L_IDLE_TRIGGER_S)
                && (sim_ < COLD_EXIT_TEMP_C) && (tb - sim_ > scalar(15.0))) {
                cold_on_ = true;
                cold_elapsed_ = scalar(0.0);
                t_front_ = sim_;
                for (int i = 0; i < n_cold_; ++i) ring_[i] = sim_;
                rpos_ = 0;
            }
            idle_timer_ = scalar(0.0);
        } else {
            idle_timer_ += DT;
            if (cold_on_ && idle_timer_ >= L_IDLE_TRIGGER_S) cold_on_ = false;
        }

        scalar tb_use;
        if (cold_on_) {
            if (pmod > scalar(0.0) && !wwb) {
                scalar delayed = ring_[rpos_];
                scalar a_cold = (p_.tau_cold_front <= scalar(0.0))
                                    ? scalar(1.0)
                                    : DT / (p_.tau_cold_front + DT);
                t_front_ = (scalar(1.0) - a_cold) * t_front_ + a_cold * delayed;
                ring_[rpos_] = tb;
                rpos_ = (rpos_ + 1) % n_cold_;
                cold_elapsed_ += DT;
            }
            tb_use = t_front_;
        } else {
            tb_use = tb;
        }

        scalar eps;
        if (p3 && !(p1 || p2)) eps = p_.eps_A;
        else if (p3 && rad_cnt == 1) eps = p_.eps_B1;
        else if (p3 && rad_cnt == 2) eps = p_.eps_B2;
        else if ((!p3) && rad_cnt > 0) eps = p_.eps_C;
        else eps = p_.eps_A;

        scalar prev = sim_;
        int branch = BR_ACTIVE;
        scalar out;

        if (wwb && pany) {
            branch = BR_DHW;
            if (dhw_timer_ < p_.dhw_delay_s) {
                out = prev;
            } else {
                scalar a = DT / (p_.tau_dhw_fast + DT);
                out = (scalar(1.0) - a) * prev + a * tret;
            }
        } else if (pany) {
            if (post_dhw_timer_ < p_.post_dhw_delay_s) {
                branch = BR_POST_DHW;
                scalar a = DT / (p_.tau_cutoff + DT);
                out = (scalar(1.0) - a) * prev + a * tret;
            } else if (pmod <= scalar(0.0)) {
                if (pump_off_timer_ < p_.L_pump_off) {
                    branch = BR_HOLD;
                    out = prev;
                } else if (p3) {
                    branch = BR_CUTOFF;
                    scalar a = DT / (p_.tau_cutoff + DT);
                    out = (scalar(1.0) - a) * prev + a * tret;
                } else {
                    branch = BR_CUTOFF;
                    scalar a = DT / (p_.tau_rad_off + DT);
                    out = (scalar(1.0) - a) * prev + a * tret;
                }
            } else if (tb <= tret + scalar(1.0)) {
                branch = BR_CUTOFF;
                scalar a = DT / (p_.tau_cutoff + DT);
                out = (scalar(1.0) - a) * prev + a * tret;
            } else if (burner_off_timer_ > scalar(0.0)) {
                branch = BR_CASING;
                scalar tb_eff;
                if (burner_off_timer_ < p_.L_burn_off) {
                    tb_eff = std::min(tb_use, tb_active_);
                } else {
                    scalar ad = DT / (p_.tau_burn_off + DT);
                    tb_active_ = (scalar(1.0) - ad) * tb_active_ + ad * tret;
                    tb_eff = std::min(tb_use, tb_active_);
                }
                scalar target = tret + eps * (tb_eff - tret);
                scalar a = DT / (p_.tau_hx + DT);
                out = (scalar(1.0) - a) * prev + a * target;
            } else {
                branch = BR_ACTIVE;
                scalar target = tret + eps * (tb_use - tret);
                scalar a = DT / (p_.tau_hx + DT);
                out = (scalar(1.0) - a) * prev + a * target;
            }
        } else {
            branch = BR_IDLE;
            scalar a = DT / (p_.tau_idle + DT);
            out = (scalar(1.0) - a) * prev + a * tr;
        }

        sim_ = out;
        t_hx_ = out;

        if (cold_on_) {
            if (sim_ >= COLD_EXIT_TEMP_C
                || cold_elapsed_ >= p_.L_cold_start
                                       + COLD_EXIT_TAU_MULT * p_.tau_cold_front) {
                cold_on_ = false;
                t_front_ = tb;
            }
        }

        branch_counts_[branch]++;
        last_branch_ = branch;
        return sim_;
    }

private:
    void init() {
        static_assert(MAX_COLD_RING_N > 0, "cold ring must be positive");
        n_cold_ = std::clamp(static_cast<int>(std::lround(p_.L_cold_start / DT)),
                             1, MAX_COLD_RING_N);
        reset(scalar(0.0), scalar(0.0));
    }

    Params p_;
    CausalMajority<32> cm1_, cm2_, cm3_;
    scalar ring_[MAX_COLD_RING_N];
    int n_cold_;
    int rpos_;
    scalar sim_;
    scalar t_hx_;
    scalar t_front_;
    scalar tb_active_;
    int consec_bp_;
    scalar dhw_timer_;
    scalar post_dhw_timer_;
    scalar pump_off_timer_;
    scalar burner_off_timer_;
    scalar idle_timer_;
    scalar cold_elapsed_;
    bool cold_on_;
    int branch_counts_[8];
    int last_branch_;
};

}  // namespace step2_v63
'''


HARNESS_TEMPLATE = r'''// Auto-generated by simulation/step2_hx_dynamics_v63.py
// Reads the parity CSV and a Params CSV, emits ep,idx,sim with %.15g.
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "step2_hx_dynamics_v63.hpp"

static void set_param(step2_v63::Params& p, const std::string& key,
                      double value) {
    if (key == "tau_hx") p.tau_hx = value;
    else if (key == "eps_A") p.eps_A = value;
    else if (key == "eps_B1") p.eps_B1 = value;
    else if (key == "eps_B2") p.eps_B2 = value;
    else if (key == "eps_C") p.eps_C = value;
    else if (key == "dhw_delay_s") p.dhw_delay_s = value;
    else if (key == "tau_dhw_fast") p.tau_dhw_fast = value;
    else if (key == "post_dhw_delay_s") p.post_dhw_delay_s = value;
    else if (key == "L_pump_off") p.L_pump_off = value;
    else if (key == "L_burn_off") p.L_burn_off = value;
    else if (key == "tau_burn_off") p.tau_burn_off = value;
    else if (key == "tau_cutoff") p.tau_cutoff = value;
    else if (key == "tau_idle") p.tau_idle = value;
    else if (key == "tau_rad_off") p.tau_rad_off = value;
    else if (key == "L_cold_start") p.L_cold_start = value;
    else if (key == "tau_cold_front") p.tau_cold_front = value;
    else {
        std::cerr << "unknown parameter: " << key << "\n";
        std::exit(2);
    }
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: " << argv[0] << " <parity.csv> <params.csv>\n";
        return 2;
    }
    step2_v63::Params params;
    {
        std::ifstream pf(argv[2]);
        if (!pf) {
            std::cerr << "cannot open " << argv[2] << "\n";
            return 2;
        }
        std::string line;
        while (std::getline(pf, line)) {
            if (line.empty()) continue;
            std::stringstream ps(line);
            std::string key, val;
            std::getline(ps, key, ',');
            std::getline(ps, val, ',');
            set_param(params, key, std::atof(val.c_str()));
        }
    }
    std::ifstream f(argv[1]);
    if (!f) {
        std::cerr << "cannot open " << argv[1] << "\n";
        return 2;
    }
    std::string line;
    std::getline(f, line);  // header
    std::string cur_ep;
    std::string ep0;
    bool first = true;
    step2_v63::Simulator* sim = nullptr;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        std::stringstream ss(line);
        std::string ep, idxs, rflag, tok;
        std::getline(ss, ep, ',');
        std::getline(ss, idxs, ',');
        std::getline(ss, rflag, ',');
        int idx = std::atoi(idxs.c_str());
        int reanchor = std::atoi(rflag.c_str());
        double v[17];
        for (int i = 0; i < 17; ++i) {
            std::getline(ss, tok, ',');
            v[i] = std::atof(tok.c_str());
        }
        if (first || ep != ep0) {
            delete sim;
            sim = new step2_v63::Simulator(params);
            sim->reset(v[1], v[0]);
            ep0 = ep;
            first = false;
        }
        if (reanchor != 0) sim->reanchor(v[1], v[0]);
        double out;
        if (idx == 0) {
            out = static_cast<double>(sim->sim());
        } else {
            // v[1] is measured t_inc, used only for re-anchoring; it is not a
            // step() input because it is the Step 2 simulated output.
            out = static_cast<double>(sim->step(
                v[0], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9],
                v[10], v[11], v[12], v[13], v[14], v[15], v[16]));
        }
        std::printf("%s,%d,%.15g\n", ep.c_str(), idx, out);
    }
    delete sim;
    return 0;
}
'''


def generate_hpp(path: Path, params: dict) -> int:
    n_cold = max(1, min(MAX_COLD_RING_N,
                         int(round(float(params["L_cold_start"]) / DT))))
    assert float(params["L_cold_start"]) / DT <= MAX_COLD_RING_N, (
        "L_cold_start exceeds MAX_COLD_RING_N * DT")
    txt = HPP_TEMPLATE
    repl = {
        "@@TAU_HX@@": f"{float(params['tau_hx']):.17g}",
        "@@EPS_A@@": f"{float(params['eps_A']):.17g}",
        "@@EPS_B1@@": f"{float(params['eps_B1']):.17g}",
        "@@EPS_B2@@": f"{float(params['eps_B2']):.17g}",
        "@@EPS_C@@": f"{float(params['eps_C']):.17g}",
        "@@DHW_DELAY@@": f"{float(params['dhw_delay_s']):.17g}",
        "@@TAU_DHW@@": f"{float(params['tau_dhw_fast']):.17g}",
        "@@POST_DHW@@": f"{float(params['post_dhw_delay_s']):.17g}",
        "@@L_PUMP_OFF@@": f"{float(params['L_pump_off']):.17g}",
        "@@L_BURN_OFF@@": f"{float(params['L_burn_off']):.17g}",
        "@@TAU_BURN_OFF@@": f"{float(params['tau_burn_off']):.17g}",
        "@@TAU_CUTOFF@@": f"{float(params['tau_cutoff']):.17g}",
        "@@TAU_IDLE@@": f"{float(params['tau_idle']):.17g}",
        "@@TAU_RAD_OFF@@": f"{float(params['tau_rad_off']):.17g}",
        "@@L_COLD_START@@": f"{float(params['L_cold_start']):.17g}",
        "@@TAU_COLD_FRONT@@": f"{float(params['tau_cold_front']):.17g}",
    }
    for k, v in repl.items():
        txt = txt.replace(k, v)
    assert "@@" not in txt, "Unreplaced placeholder in generated header"
    path.write_text(txt)
    return n_cold


def write_parity_csv(path: Path, episodes: dict[str, pd.DataFrame],
                     unit_frames: dict[str, pd.DataFrame],
                     reanchor_map: dict[str, int]) -> None:
    with open(path, "w") as f:
        f.write("ep,idx,reanchor," + ",".join(SIM_INPUT_COLUMNS) + "\n")
        for eid, df in list(episodes.items()) + list(unit_frames.items()):
            cols = [df[c].values for c in SIM_INPUT_COLUMNS]
            ri = int(reanchor_map.get(eid, 0))
            for i in range(len(df)):
                vals = ",".join(f"{float(c[i]):.17g}" for c in cols)
                f.write(f"{eid},{i},{1 if (ri > 0 and i == ri) else 0},{vals}\n")


def write_params_csv(path: Path, params: dict) -> None:
    order = ("tau_hx", "eps_A", "eps_B1", "eps_B2", "eps_C", "dhw_delay_s",
             "tau_dhw_fast", "post_dhw_delay_s", "L_pump_off", "L_burn_off",
             "tau_burn_off", "tau_cutoff", "tau_idle", "tau_rad_off",
             "L_cold_start", "tau_cold_front")
    path.write_text("\n".join(f"{k},{float(params[k]):.17g}" for k in order) + "\n")


def run_cpp_parity(out_dir: Path, episodes: dict[str, pd.DataFrame],
                   unit_frames: dict[str, pd.DataFrame], params: dict) -> dict:
    hpp_path = out_dir / HPP_NAME
    cpp_path = out_dir / HARNESS_NAME
    bin_path = out_dir / HARNESS_BIN_NAME
    csv_path = out_dir / PARITY_CSV_NAME

    n_cold = generate_hpp(hpp_path, params)
    cpp_path.write_text(HARNESS_TEMPLATE)
    reanchor_map = {eid: (0 if eid.startswith("FIT") else WARMUP_STEPS)
                    for eid in episodes}
    reanchor_map.update({eid: 0 for eid in unit_frames})
    write_parity_csv(csv_path, episodes, unit_frames, reanchor_map)

    gxx = shutil.which("g++")
    assert gxx is not None, "g++ is required for the Python/C++ parity harness"
    compile_cmd = [gxx, "-O2", "-std=c++17", "-o", str(bin_path), str(cpp_path)]
    comp = subprocess.run(compile_cmd, cwd=str(out_dir), capture_output=True,
                          text=True)
    assert comp.returncode == 0, f"C++ compilation failed:\n{comp.stderr}"

    perturbed = dict(params)
    perturbed.update(L_cold_start=200.0, L_pump_off=0.0, tau_cold_front=20.0)

    results = {}
    for label, p in (("default", params), ("perturbed", perturbed)):
        pfile = out_dir / f"{PARITY_PARAMS_NAME}.{label}"
        write_params_csv(pfile, p)
        run = subprocess.run([str(bin_path), str(csv_path), str(pfile)],
                             cwd=str(out_dir), capture_output=True, text=True)
        assert run.returncode == 0, f"C++ harness failed:\n{run.stderr}"
        cpp = {}
        for line in run.stdout.splitlines():
            if not line:
                continue
            ep, idxs, val = line.split(",")
            cpp.setdefault(ep, {})[int(idxs)] = float(val)

        max_diff = 0.0
        worst = None
        total_rows = 0
        for eid, df in list(episodes.items()) + list(unit_frames.items()):
            res = simulate_episode(df, p, mode="v6",
                                   reanchor_index=reanchor_map.get(eid, 0))
            sim = res["sim"]
            assert eid in cpp, f"C++ output missing episode {eid}"
            assert len(cpp[eid]) == len(df), f"C++ row count mismatch for {eid}"
            for i in range(len(df)):
                d = abs(float(sim[i]) - cpp[eid][i])
                total_rows += 1
                if d > max_diff:
                    max_diff = d
                    worst = (eid, i, float(sim[i]), cpp[eid][i])
        assert max_diff < 1e-9, (
            f"Python/C++ parity failed for {label} parameters: "
            f"max|diff|={max_diff:.3e} at {worst}")
        results[label] = dict(rows=total_rows, max_abs_diff_c=max_diff,
                              worst=worst, params=p)

    return dict(
        compiled=True, compiler=gxx, flags=["-O2", "-std=c++17"],
        header=str(hpp_path.name), harness=str(cpp_path.name),
        csv=str(csv_path.name), binary=str(bin_path.name),
        cold_ring_n=n_cold,
        rows=results["default"]["rows"],
        max_abs_diff_c=max(results[l]["max_abs_diff_c"] for l in results),
        tolerance_c=1e-9,
        default=dict(rows=results["default"]["rows"],
                     max_abs_diff_c=results["default"]["max_abs_diff_c"],
                     worst=results["default"]["worst"]),
        perturbed=dict(rows=results["perturbed"]["rows"],
                       max_abs_diff_c=results["perturbed"]["max_abs_diff_c"],
                       worst=results["perturbed"]["worst"],
                       parameters=dict(L_cold_start=200.0, L_pump_off=0.0,
                                       tau_cold_front=20.0)),
        worst=results["default"]["worst"],
    )


# ---------------------------------------------------------------------------
# Artifact serialisation
# ---------------------------------------------------------------------------
def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()
                if k not in ("sim", "tinc_true", "tinc_age", "df")}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _fmt(x, nd=3):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "n/a"
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def dhw_plunge_mask(n: int, ww: np.ndarray,
                    min_on_steps: int = 60,
                    plunge_steps: int = 60) -> np.ndarray:
    """Explicit DHW plunge windows: every ww 1->0 edge after >= 10 min on."""
    mask = np.zeros(n, dtype=bool)
    i = 1
    while i < n:
        if ww[i - 1] >= 0.5 and ww[i] < 0.5:
            j = i - 1
            while j > 0 and ww[j - 1] >= 0.5:
                j -= 1
            if (i - j) >= min_on_steps:
                mask[i:min(n, i + plunge_steps)] = True
        i += 1
    return mask


def suite_scores(records: list[dict], mask_fn) -> dict:
    eps = []
    sims, truths = [], []
    for r in records:
        df = r["df"]
        n = len(df)
        ww = df["ww_flag"].values
        base = np.zeros(n, dtype=bool)
        base[WARMUP_STEPS:] = True
        base &= r["tinc_age"] <= TB_MAX_AGE_S
        base &= ~mask_fn(n, ww)
        base &= ~(ww >= 0.5)
        e = r["sim"][base] - r["tinc_true"][base]
        t = r["tinc_true"][base]
        if len(t) == 0:
            continue
        rmse = float(np.sqrt(np.mean(e * e)))
        mae = float(np.mean(np.abs(e)))
        ss = float(np.sum((t - np.mean(t)) ** 2))
        r2 = float(1.0 - np.sum(e * e) / ss) if ss > 0 else float("nan")
        eps.append(dict(id=r["id"], rmse=rmse, mae=mae, r2=r2,
                        samples=int(len(t))))
        sims.append(r["sim"][base])
        truths.append(t)
    sim_all = np.concatenate(sims) if sims else np.array([])
    true_all = np.concatenate(truths) if truths else np.array([])
    ee = sim_all - true_all
    ss = float(np.sum((true_all - np.mean(true_all)) ** 2)) if len(true_all) else 0.0
    return dict(
        episodes=eps,
        mean_rmse=float(np.mean([e["rmse"] for e in eps])) if eps else float("nan"),
        mean_mae=float(np.mean([e["mae"] for e in eps])) if eps else float("nan"),
        pooled_rmse=float(np.sqrt(np.mean(ee * ee))) if len(ee) else float("nan"),
        pooled_r2=float(1.0 - np.sum(ee * ee) / ss) if ss > 0 else float("nan"),
        min_r2=float(np.min([e["r2"] for e in eps])) if eps else float("nan"),
        samples=int(len(true_all)),
    )


def gate_rows(gates: dict, sh: dict, dhw: dict) -> list[dict]:
    specs = [
        ("space_heating", "Mean episode RMSE (degC)",
         gates["space_heating_suite"]["metrics"]["mean_episode_rmse_c"], sh["mean_rmse"]),
        ("space_heating", "Pooled RMSE (degC)",
         gates["space_heating_suite"]["metrics"]["pooled_rmse_c"], sh["pooled_rmse"]),
        ("space_heating", "Mean episode MAE (degC)",
         gates["space_heating_suite"]["metrics"]["mean_episode_mae_c"], sh["mean_mae"]),
        ("space_heating", "Minimum per-episode R2",
         gates["space_heating_suite"]["metrics"]["min_episode_r2"], sh["min_r2"]),
        ("dhw", "DHW plunge RMSE (degC)",
         gates["dhw_suite"]["metrics"]["plunge_rmse_c"], dhw["rmse"]),
        ("dhw", "DHW per-episode R2",
         gates["dhw_suite"]["metrics"]["min_episode_r2"], dhw["r2"]),
    ]
    out = []
    for suite, label, spec, actual in specs:
        thr = float(spec["threshold"])
        if spec["op"] == "<":
            passed = actual < thr
            margin = thr - actual
        else:
            passed = actual > thr
            margin = actual - thr
        frac = margin / thr if thr else float("nan")
        out.append(dict(suite=suite, label=label, op=spec["op"], threshold=thr,
                        actual=actual, passed=bool(passed),
                        margin=margin, margin_fraction=frac,
                        marginal=bool(abs(frac) < gates["marginal_margin_fraction"])))
    return out


def build_report(path: Path, ctx: dict) -> None:
    fit = ctx["fit_ratios"]
    hx = ctx["hx_fit"]
    dhw = ctx["dhw_fit"]
    cold = ctx["cold_fit"]
    params = ctx["params"]
    prov = ctx["provenance"]
    tune = ctx["tune"]
    tune_metrics = ctx["tune_metrics"]
    val = ctx["val_records"]
    hold = ctx["holdout_records"]
    five_t1 = ctx["five_tune1"]
    five_t3 = ctx["five_tune3"]
    parity = ctx["parity"]
    unit = ctx["unit_tests"]
    gates = ctx["gates"]
    freeze = ctx["freeze"]
    sh = ctx["holdout_sh"]
    dhw_score = ctx["holdout_dhw"]
    gate_eval = ctx["gate_eval"]
    param_hash = ctx["param_hash"]
    commit = ctx["commit"]
    stale = ctx["stale_counts"]
    v62_attr = ctx["v62_attribution"]
    dhw6 = ctx["dhw6_details"]
    sr = ctx["sensor_res"]
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    overall = "PASS" if all(g["passed"] for g in gate_eval) else "FAIL"

    val_rows = [[r["id"], r["name"], r["samples"], r["excluded_samples"],
                 _fmt(r["tinc_max_age_s"], 0), _fmt(r["rmse"]), _fmt(r["mae"]),
                 _fmt(r["p95"]), _fmt(r["r2"], 4)] for r in val]
    hold_rows = [[r["id"], r["name"], r["samples"], _fmt(r["rmse"]),
                  _fmt(r["mae"]), _fmt(r["r2"], 4)] for r in hold]

    tune_rows = [[eid, tune_metrics[eid]["samples"],
                  _fmt(tune_metrics[eid]["rmse"]), _fmt(tune_metrics[eid]["mae"]),
                  _fmt(tune_metrics[eid]["r2"], 4), _fmt(tune_metrics[eid]["bias"])]
                 for eid in tune["tune_ids"]]

    hist_rows = [[h["pass_no"], h["parameter"],
                  "n/a" if h["value"] is None else _fmt(h["value"], 1),
                  _fmt(h["objective"], 6),
                  _fmt(h["tune_rmse"][0], 4), _fmt(h["tune_rmse"][1], 4),
                  _fmt(h["tune_rmse"][2], 4)] for h in tune["history"]]

    param_order = ["tau_hx", "eps_A", "eps_B1", "eps_B2", "eps_C",
                   "dhw_delay_s", "tau_dhw_fast", "post_dhw_delay_s",
                   "L_pump_off", "L_burn_off", "tau_burn_off", "tau_cutoff",
                   "L_cold_start", "tau_cold_front", "tau_rad_off", "tau_idle"]
    param_prec = {n: 6 for n in ("eps_A", "eps_B1", "eps_B2", "eps_C")}
    param_prec.update({"tau_hx": 4, "tau_dhw_fast": 4})
    param_rows = [[f"`{n}`", _fmt(params[n], param_prec.get(n, 1)), prov[n]]
                  for n in param_order]

    gate_table = []
    for g in gate_eval:
        gate_table.append([
            g["suite"], g["label"], f"{g['op']} {g['threshold']:g}",
            _fmt(g["actual"], 4), "PASS" if g["passed"] else "FAIL",
            _fmt(g["margin"], 4),
            f"{g['margin_fraction'] * 100.0:+.2f}%",
            "marginal" if g["marginal"] else "",
        ])

    def five_rows(five):
        rows = []
        for label, r in five["results"].items():
            rows.append([label, _fmt(r["overall"]["rmse"]),
                         r["overall"]["samples"],
                         _fmt(r["cold"]["rmse"]), r["cold"]["samples"],
                         _fmt(r["cutoffs"]["rmse"]), r["cutoffs"]["samples"],
                         _fmt(r["final"]["rmse"]), r["final"]["samples"]])
        return rows

    fw_headers = ["Model", "Overall RMSE", "N", "Cold-start RMSE", "N",
                  "Mid-run cutoffs RMSE", "N", "Final shutdown RMSE", "N"]

    ut_rows = [[t["name"], "PASS" if t["passed"] else "FAIL", t["expected"],
                t["detail"]] for t in unit]

    corr = fit["fit_regime_a_correlations"]
    full = fit["fit_full_window_correlations"]
    stale_rows = [[k, v] for k, v in stale.items()]

    cold_durations = ctx["cold_durations"]
    cold_dur_txt = ", ".join(
        f"{d['episode']} {d['model']} {d['duration_s']:.0f} s"
        for d in cold_durations) or "no TUNE cold start"

    dhw6_txt = (
        f"measured entry delay {dhw6['measured_delay_s']:.0f} s versus simulated "
        f"{dhw6['simulated_delay_s']:.0f} s, measured plunge depth "
        f"{dhw6['measured_depth_c']:.2f} degC versus simulated "
        f"{dhw6['simulated_depth_c']:.2f} degC"
    )

    rad_stats = fit["tau_rad_off_stats"]
    if rad_stats["segments"]:
        rad_txt = (
            f"`tau_rad_off` = {_fmt(fit['tau_rad_off'], 1)} s (median, IQR "
            f"{_fmt(rad_stats['iqr_s'], 1)} s, N = {rad_stats['segments']} "
            "segments, each at least 20 minutes). The legacy one-step ratio "
            f"gives {_fmt(fit['tau_rad_off_legacy'], 1)} s."
        )
    else:
        rad_txt = (
            "No radiator-only segment met the criteria (at least 20 minutes, "
            "fresh telemetry, at least 10 degC start excess over T_return), so "
            "`tau_rad_off` falls back to the legacy one-step estimate: "
            f"{_fmt(fit['tau_rad_off'], 1)} s."
        )

    if fit["room_offset_adopted"]:
        offset_txt = (
            "The scan optimum is interior and reduces the FIT residual, so the "
            "offset is adopted."
        )
    elif fit["room_offset_boundary"]:
        offset_txt = (
            "The scan optimum sits on the grid boundary, so it is treated as "
            "absorbing model mismatch rather than sensor bias, and the offset "
            "is kept at zero."
        )
    else:
        offset_txt = (
            "The scan optimum is interior but does not reduce the FIT "
            "residual, so the offset is kept at zero."
        )

    n_passes = max(h["pass_no"] for h in tune["history"])
    marginal_ids = [g["label"] for g in gate_eval if g["marginal"]]
    marginal_txt = (", ".join(marginal_ids) if marginal_ids
                    else "none of the gates is marginal")

    md = f"""# Phase 2 - Version 6.3: Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)

**Project:** Hardware-in-the-Loop (HIL) Floor Heating Simulator
**Version:** {VERSION}
**Generated:** {ts}
**Script:** `simulation/step2_hx_dynamics_v63.py`
**Database:** `analysis/heatpumps.duckdb` (read-only)
**Generated C++:** `simulation/{HPP_NAME}`

## 0. Sign-off summary

**Holdout result: {overall}.** The Phase 0 holdout ({", ".join(HOLDOUT_IDS)})
was selected from telemetry only, frozen and committed before any model change,
and evaluated once after the parameter freeze. Its result is reported as is.

| Provenance item | Value |
|---|---|
| Phase 0 commit | `{freeze.get("phase0_commit", "n/a")}` |
| Report commit | `{commit}` |
| `gates_v63.json` SHA-256 | `{freeze["gates_sha256"]}` |
| `holdout_v63.json` SHA-256 | `{freeze["holdout_sha256"]}` |
| Final parameter dict SHA-256 | `{param_hash}` |

VAL-5 (2026-02-26) is the **observed holdout (seen in v6.2)**. It is evaluated
and reported in Section 5, but it cannot support a sign-off. The untouched
subset is exactly the Phase 0 holdout.

### 0.0 Frozen gates

{_table(["Suite", "Metric", "Threshold", "Actual", "Result", "Margin", "Margin %", "Note"], gate_table)}

A margin below {gates["marginal_margin_fraction"] * 100.0:g}% of the threshold is
labelled marginal; {marginal_txt}.

### 0.1 Attribution of the observed v6.2 VAL-5 failure

The Phase 1 per-branch and per-window breakdown of the v6.2 parameters is
recomputed in this run:

{_table(["Branch", "Samples", "RMSE (degC)", "Bias (degC)"], [[r["branch"], r["samples"], _fmt(r["rmse_c"]), _fmt(r["bias_c"])] for r in v62_attr["branches"]])}

{_table(["Window", "Samples", "RMSE (degC)", "Bias (degC)"], [[r["window"], r["samples"], _fmt(r["rmse_c"]), _fmt(r["bias_c"])] for r in v62_attr["windows"]])}

The largest contributions are the {v62_attr["worst_branch"]} branch
({_fmt(v62_attr["worst_branch_rmse"])} degC) and the
{v62_attr["worst_window"]} window ({_fmt(v62_attr["worst_window_rmse"])} degC).
Re-running the same v6.2 parameters with the exact v6.2 staleness rule holds
{v62_attr["stale_samples"]} VAL-5 samples; the v6.3 rule holds
{v62_attr["stale_samples_v63"]}. Phase 1 showed the v6.2 holds were caused by
event-driven burner/pump ages while the analog signals were fresh, which is why
v6.3 moves those signals to the event-style rule.

## 1. Split and disjointness

All FIT, TUNE, observed VAL and Phase 0 holdout episodes are strictly disjoint,
including the 30-minute warm-up buffers. The assertion is executed in
`main()` and uses the same episode registry as the evaluation. FIT-DHW is a
dedicated sub-window of the FIT period; its rows are excluded from the FIT
ratio identification so that the two FIT windows do not overlap in any
estimator.

| Partition | Episode | Window | Role |
|---|---|---|---|
""" + "\n".join(
        f"| {meta['kind']} | {eid} | {meta['t_start']} -> {meta['t_end']} | {meta['role']} |"
        for eid, meta in EPISODES.items()) + f"""

Observed episodes (VAL-1..VAL-7) were seen in v5/v6.1/v6.2 and are labelled
"previously observed". Only the Phase 0 holdout is untouched.

## 2. Model

The HX outlet is a first-order lag toward the secondary-return-referenced
target:

```
T_hx[k] = (1 - a) * T_hx[k-1] + a * (T_return[k] + eps * (T_boiler,eff[k] - T_return[k]))
a = DT / (tau_hx + DT)
```

The v6.3 branch structure is unchanged from v6. The changes are in
identification, staleness, window detection and reporting. Pump states are
debounced with the same strictly causal trailing majority used by the C++
header.

### 2.1 Staleness

Telemetry is valid when `tb_age_s <= {TB_MAX_AGE_S:g}` and
`tret_age_s <= {TB_MAX_AGE_S:g}`. Burner, pump modulation and all pump-power
ages are event-driven: they invalidate a sample only when their own age exceeds
`{EVT_MAX_AGE_S:g}` s **and** `tb_age_s` is also stale. Target metrics exclude
stale measured `T_incoming` samples. Stale-hold counts per episode in this run:

{_table(["Episode", "stale_hold samples"], stale_rows)}

The VAL-5 failure mechanism (event age without analog staleness) is therefore
removed, while genuine gateway dropouts still hold the simulator state.

## 3. FIT identification (FIT data only)

### 3.1 Effectiveness (input-only mask)

The v6.3 mask uses inputs only: `bp > 5`, `tb - tret > 10`, all pump states
stable for at least five minutes, no DHW, and fresh telemetry. Old and new
estimates:

{_table(["Regime", "New N", "New eps", "Old N", "Old eps"], [[k, fit["new"][k]["count"], _fmt(fit["new"][k]["eff_mean"], 4), fit["old"][k]["count"], _fmt(fit["old"][k]["eff_mean"], 4)] for k in ("A", "B1", "B2", "C")])}

Ratios relative to Regime A: new B1/A = {fit["new"]["ratio_B1"]:.4f},
B2/A = {fit["new"]["ratio_B2"]:.4f}, C/A = {fit["new"]["ratio_C"]:.4f};
old B1/A = {fit["old"]["ratio_B1"]:.4f}, B2/A = {fit["old"]["ratio_B2"]:.4f},
C/A = {fit["old"]["ratio_C"]:.4f}. The FIT-1 `tau_hx`/`eps_A` loss uses the same
input-based mask.

### 3.2 `tau_hx` and `eps_A`

| Parameter | Value | FIT-1 RMSE (degC) | Fresh samples | Excluded stale |
|---|---|---|---|---|
| `tau_hx` | {_fmt(hx['tau_hx'], 4)} s | {_fmt(hx['rmse'], 4)} | {hx['samples']:,} | {hx['excluded_stale']} |
| `eps_A` | {_fmt(hx['eps_A'], 6)} | {_fmt(hx['rmse'], 4)} | {hx['samples']:,} | {hx['excluded_stale']} |

FIT-1 now ends before VAL-4's warm-up begins, so the identification window no
longer overlaps the validation episode.

### 3.3 DHW diverter (FIT-DHW)

`dhw_delay_s = {_fmt(dhw['dhw_delay_s'], 1)} s`, `tau_dhw_fast =
{_fmt(dhw['tau_dhw_fast'], 4)} s`, FIT-DHW entry RMSE = {_fmt(dhw['rmse'], 4)} degC.
The final DHW timing is tuned on TUNE only; FIT-DHW is not part of the tuning
objective. Its RMSE at the final value is reported in Section 4.

### 3.4 Cooling constants from per-segment exponential fits

{rad_txt}

`tau_idle` = {_fmt(fit['tau_idle'], 1)} s (median, IQR
{_fmt(fit['tau_idle_stats']['iqr_s'], 1)} s, N = {fit['tau_idle_stats']['segments']}
segments). The legacy one-step ratio gives {_fmt(fit['tau_idle_legacy'], 1)} s.
The room-sensor offset scan ranges over {fit['room_offset_range'][0]:+g} to
{fit['room_offset_range'][1]:+g} degC. The adopted offset is
{fit['room_offset_c']:+.2f} degC. The zero-offset residual is
{_fmt(fit['room_offset_sse_zero'], 1)}; the best value anywhere in the scan
gives {_fmt(fit['room_offset_sse'], 1)}, and the residual of the adopted value
is {_fmt(fit['room_offset_sse_adopted'], 1)}. {offset_txt}

### 3.5 Hydraulic correlations

Settled Regime A (stable for at least ten minutes, mixer state unchanged for at
least five minutes, N = {corr['samples']:,}):

| Correlation | Settled | Full FIT window |
|---|---|---|
| `r(mixer_state, T_incoming)` | {corr['r_mstate_tinc']:+.4f} | {full['r_mstate_tinc']:+.4f} |
| `r(mixer_state, dT_hx)` | {corr['r_mstate_dthx']:+.4f} | {full['r_mstate_dthx']:+.4f} |
| `r(T_boiler, T_incoming)` | {corr['r_tb_tinc']:+.4f} | n/a |

The no-material-linear-association claim is limited to `dT_hx`. The mixer state
is materially associated with `T_incoming` on the same settled samples, so the
claim is not extended to the outlet temperature.

## 4. TUNE results and parameters

The tuning objective is the equal-weight mean target-window RMSE over
TUNE-1..TUNE-3. DHW timing is tuned on TUNE only. `tau_rad_off` and `tau_idle`
stay at their FIT values.

### 4.1 Cold-start front

`L_cold_start = {_fmt(cold['L_cold_start'], 1)} s`, `tau_cold_front =
{_fmt(cold['tau_cold_front'], 1)} s`, fitted on all TUNE cold starts over the
measured transient length ({ctx['cold_fit_steps']} samples). Cold-window RMSE =
{_fmt(cold['rmse'], 4)} degC. Measured transient lengths: {cold_dur_txt}.
`tau_cold_front` is constrained to at least one DT sample.

### 4.2 Coordinate search

The coordinate search ran {n_passes} passes; the accepted moves are listed
below.

{_table(["Pass", "Parameter", "Value", "Objective", "TUNE-1", "TUNE-2", "TUNE-3"], hist_rows)}

Boundary-limited parameters:
{", ".join(k for k, v in tune["boundary_limited"].items() if v) or "none"} (a
value at the zero floor means the hold is disabled). `L_burn_off < DT`
disables the casing hold; the effective value is
`{_fmt(params['L_burn_off'], 1)}` s.

### 4.3 Final TUNE metrics

{_table(["Episode", "Samples", "RMSE (degC)", "MAE (degC)", "R2", "Bias (degC)"], tune_rows)}

TUNE-1 is in-sample for the cold-start parameters. All TUNE residuals are
optimisation diagnostics, not out-of-sample evidence.

### 4.4 Final parameters

{_table(["Parameter", "Value", "Provenance"], param_rows)}

FIT-DHW RMSE at the final parameter dict: {ctx['fit_dhw_final_rmse']:.4f} degC.

## 5. Observed validation (previously seen)

{_table(["ID", "Episode", "Samples", "Excluded", "Max tinc age (s)", "RMSE (degC)", "MAE (degC)", "P95 (degC)", "R2"], val_rows)}

These episodes are reported for continuity but are not sign-off evidence.
Pooled RMSE is shown beside the mean RMSE, and per-episode R2 beside the pooled
R2, because pooling gives long low-variance episodes more weight: the
per-episode R2 range here is
{_fmt(min(r['r2'] for r in val), 4)} to {_fmt(max(r['r2'] for r in val), 4)}.

## 6. Phase 0 holdout (evaluated once)

{_table(["ID", "Episode", "Scored samples", "RMSE (degC)", "MAE (degC)", "R2"], hold_rows)}

Space-heating suite ({", ".join(gates['space_heating_suite']['episodes'])},
excluding DHW-on samples and DHW plunge windows): mean RMSE =
{_fmt(sh['mean_rmse'], 4)} degC, pooled RMSE = {_fmt(sh['pooled_rmse'], 4)} degC,
mean MAE = {_fmt(sh['mean_mae'], 4)} degC, minimum per-episode R2 =
{_fmt(sh['min_r2'], 4)}, pooled R2 = {_fmt(sh['pooled_r2'], 4)}.

DHW suite ({", ".join(gates['dhw_suite']['episodes'])}): plunge-window RMSE =
{_fmt(dhw_score['rmse'], 4)} degC over {dhw_score['samples']} samples, R2 =
{_fmt(dhw_score['r2'], 4)}.

The gate table in Section 0.0 is the complete pass/fail outcome. No parameter
or structure was changed after this evaluation.

## 7. Five-way comparison

### 7.1 TUNE-1

{_table(fw_headers, five_rows(five_t1))}

The cold-start window is sized from the measured transient, mid-run cutoffs
require a relight, and the final shutdown includes the run-down. TUNE-1 is
in-sample for the cold-start parameters. The v6.3 architecture row uses the
fitted cold-start values rather than literals.

### 7.2 TUNE-3

{_table(fw_headers, five_rows(five_t3))}

The pure-architecture and combined changes are reported per window; the tables
above carry the sample count for every window so that a small-window RMSE is
not mistaken for an episode-level result.

## 8. Python/C++ parity and unit tests

| Parity item | Result |
|---|---|
| Compiler | `{parity['compiler']}` |
| Flags | `{ ' '.join(parity['flags']) }` |
| Rows per parameter set | {parity['rows']:,} |
| Default parameter set max abs diff | `{parity['default']['max_abs_diff_c']:.3e}` degC |
| Perturbed parameter set max abs diff | `{parity['perturbed']['max_abs_diff_c']:.3e}` degC |
| Tolerance | `< {parity['tolerance_c']:g}` degC |
| Perturbed set | `L_cold_start={parity['perturbed']['parameters']['L_cold_start']:g}`, `L_pump_off={parity['perturbed']['parameters']['L_pump_off']:g}`, `tau_cold_front={parity['perturbed']['parameters']['tau_cold_front']:g}` |
| Cold-start ring length | {parity['cold_ring_n']} samples |

{_table(["Synthetic test", "Result", "Expected", "Detail"], ut_rows)}

UT6 exercises the single-step firing glitch and UT7 the low-lift Branch 2B; both
include the legacy prediction that must fail, so they are not vacuous.

## 9. Limitations and residual risks

- **Holdout outcome.** The frozen gates are {overall}. The gate margins above
  are the only acceptance statement in this report.
- **Observed VAL-5.** The observed holdout remains a residual diagnostic. Its
  v6.2 failure was attributed in Section 0.1 to the branches and windows shown
  there, not to a generic statement about the model.
- **DHW timing.** On VAL-6 the measured versus simulated entry/plunge comparison
  is {dhw6_txt}. The DHW objective is TUNE-only, so this remains an observed
  residual.
- **Idle cooling and radiator-only stagnation.** `tau_idle` and `tau_rad_off`
  are single global constants fitted from per-segment exponential decays; the
  IQRs in Section 3.4 show the segment-to-segment spread.
- **Sensor resolution.** Raw resolutions measured in this run: mixer incoming
  {sr['mixer_incoming']:.4g} degC, mixer return {sr['mixer_return']:.4g} degC,
  room {sr['room']:.4g} degC and boiler flow {sr['boiler_flow']:.4g} degC.
  A quantised plateau is not evidence that the true temperature is constant.
- **Causal pump filters.** The pump states are debounced with a trailing
  majority, so a real pump transition is represented after the majority flips;
  the offline centred-filter comparison in earlier versions was diagnostic only
  and is not a causal result.

## 10. Reproduction

```bash
uv run python simulation/select_holdout_v63.py   # Phase 0 (already frozen)
uv run python simulation/diag_v63.py             # Phase 1 diagnostics
uv run python simulation/step2_hx_dynamics_v63.py
```

## 11. Artifacts

| Artifact | Purpose |
|---|---|
| `simulation/{PARAMS_JSON_NAME}` | v6.3 parameter and metric export |
| `simulation/{HPP_NAME}` | Generated C++ implementation |
| `simulation/{HARNESS_NAME}` | Parity harness |
| `simulation/{PARITY_CSV_NAME}` | Row-level parity input |
| `simulation/{HARNESS_BIN_NAME}` | Compiled harness |
| `simulation/{REPORT_NAME}` | This report |
| `simulation/{PLOT_NAME}` | TUNE-1 diagnostic overlay |
| `simulation/holdout_v63.json` | Frozen Phase 0 holdout |
| `simulation/gates_v63.json` | Frozen gates |
| `simulation/freeze_v63.json` | Frozen hashes and commit |
| `simulation/diag_v63/` | Phase 1 diagnostics |
| `simulation/v63_notes.md` | Findings, decisions and audit mapping |
"""
    path.write_text(md)


# ---------------------------------------------------------------------------
# Diagnostic plot
# ---------------------------------------------------------------------------
def plot_tune1(path: Path, five: dict) -> None:
    df = five["df"]
    sim_v6 = five["results"][FIVE_WAY_LABELS[4]]["sim"]
    sim_arch = five["results"][FIVE_WAY_LABELS[3]]["sim"]
    sim_v5 = five["results"][FIVE_WAY_LABELS[2]]["sim"]
    t = np.arange(len(df)) * DT / 60.0
    t0 = WARMUP_STEPS * DT / 60.0
    truth = df["t_inc"].values
    meta = EPISODES[five["eid"]]

    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(15, 11), sharex=True,
        gridspec_kw={"height_ratios": [2.6, 1.5, 1.2]},
    )
    ax1.plot(t, df["t_boiler"].values, color="crimson", lw=1.4,
             label="Boiler flow T_boiler", alpha=0.85)
    ax1.plot(t, truth, color="black", lw=2.0,
             label="Measured incoming T_incoming", alpha=0.9)
    ax1.plot(t, sim_v6, color="dodgerblue", lw=1.8, ls="--",
             label="v6.3 final")
    ax1.plot(t, sim_arch, color="teal", lw=1.1, ls="-.",
             label="v6.3 architecture (frozen v5 constants)", alpha=0.8)
    ax1.plot(t, sim_v5, color="gray", lw=1.2, ls=":",
             label="v5 simulated")
    ax1.plot(t, df["t_ret"].values, color="forestgreen", lw=1.1,
             label="Secondary return T_return", alpha=0.7)

    cold = five["windows"]["cold"]
    if cold:
        ax1.axvspan(cold[0] * DT / 60.0, cold[1] * DT / 60.0,
                    color="orange", alpha=0.18, label="Cold start")
    for i, (a, b) in enumerate(five["windows"]["cutoffs"]):
        ax1.axvspan(a * DT / 60.0, b * DT / 60.0, color="purple", alpha=0.13,
                    label="Mid-run cutoff (relight)" if i == 0 else None)
    for i, (a, b) in enumerate(five["windows"].get("rundowns", [])):
        ax1.axvspan(a * DT / 60.0, b * DT / 60.0, color="saddlebrown",
                    alpha=0.13, label="Run-down" if i == 0 else None)
    if five["windows"]["final"]:
        a, b = five["windows"]["final"]
        ax1.axvspan(a * DT / 60.0, b * DT / 60.0, color="red", alpha=0.18,
                    label="Final shutdown (incl. run-down)")
    ax1.axvline(t0, color="black", lw=0.8, ls="-.")
    ax1.set_ylabel("Temperature (degC)", fontsize=11, fontweight="bold")
    ax1.set_title(f"{meta['name']}: v6.3 / architecture / v5 overlay",
                  fontsize=13, fontweight="bold")
    ax1.grid(True, ls=":", alpha=0.6)
    ax1.legend(loc="upper left", ncol=3, fontsize=9)

    ax2.plot(t, sim_v5 - truth, color="gray", lw=1.2, ls=":",
             label="v5 error")
    ax2.plot(t, sim_v6 - truth, color="darkorchid", lw=1.5,
             label="v6.3 error")
    ax2.axhline(0, color="black", lw=0.8)
    ax2.axhline(+2.0, color="gray", lw=1.0, ls=":")
    ax2.axhline(-2.0, color="gray", lw=1.0, ls=":")
    ax2.fill_between(t, sim_v6 - truth, 0,
                     where=np.abs(sim_v6 - truth) > 2.0,
                     color="red", alpha=0.25)
    ax2.set_ylabel("Error (degC)", fontsize=11, fontweight="bold")
    ax2.grid(True, ls=":", alpha=0.6)
    ax2.legend(loc="upper left", ncol=2, fontsize=9)

    ax3_twin = ax3.twinx()
    p1 = ax3.plot(t, df["burner_pct"].values, color="darkorange", lw=1.3,
                  label="Burner modulation (%)")
    p2 = ax3.plot(t, df["pump_mod"].values, color="green", lw=1.1, ls="--",
                  label="Primary pump modulation (%)")
    p3 = ax3_twin.plot(t, df["p3_w"].values, color="royalblue", lw=1.3,
                       label="Floor pump 3 (W)")
    ax3.set_ylabel("Modulation (%)", color="darkorange", fontsize=10,
                   fontweight="bold")
    ax3_twin.set_ylabel("Pump 3 (W)", color="royalblue", fontsize=10,
                        fontweight="bold")
    ax3.set_xlabel("Elapsed time from warm-up start (minutes)", fontsize=11,
                   fontweight="bold")
    ax3.grid(True, ls=":", alpha=0.6)
    lines = p1 + p2 + p3
    ax3.legend(lines, [l.get_label() for l in lines], loc="upper right",
               fontsize=9)
    plt.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def assert_disjoint() -> list[dict]:
    """Assert that no FIT/TUNE/VAL/holdout episodes or warm-ups overlap.

    FIT-DHW is deliberately nested inside the FIT period; it is a dedicated
    sub-window and its rows are excluded from the FIT ratio estimator.  That is
    the only permitted nesting, and it never crosses a partition.
    """
    warm = pd.Timedelta(minutes=WARMUP_MIN)
    spans = []
    for eid, meta in EPISODES.items():
        spans.append(dict(name=eid, kind=meta["kind"],
                          start=pd.Timestamp(meta["t_start"]) - warm,
                          end=pd.Timestamp(meta["t_end"]) + warm))
    spans.append(dict(name="FIT_WINDOW", kind="FIT",
                      start=pd.Timestamp(FIT_WINDOW["t_start"]) - warm,
                      end=pd.Timestamp(FIT_WINDOW["t_end"]) + warm))
    overlaps = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            x, y = spans[i], spans[j]
            if x["start"] < y["end"] and y["start"] < x["end"]:
                nested = ((x["start"] <= y["start"] and y["end"] <= x["end"])
                          or (y["start"] <= x["start"] and x["end"] <= y["end"]))
                if x["kind"] == "FIT" and y["kind"] == "FIT" and nested:
                    overlaps.append(dict(first=x["name"], second=y["name"],
                                         allowed_nested_fit=True))
                    continue
                raise AssertionError(
                    f"Split overlap including warm-ups: {x['name']} and "
                    f"{y['name']}")
    return overlaps


def val5_attribution(con, reference_params: dict) -> dict:
    """Recompute the Phase 1.1 v6.2 VAL-5 branch/window breakdown in-run."""
    df = load_episode(con, EPISODES["VAL-5"]["t_start"],
                      EPISODES["VAL-5"]["t_end"])
    res = simulate_episode(df, reference_params, mode="v6",
                           reanchor_index=WARMUP_STEPS,
                           stale_rule="v62")
    sim, truth, age, br = res["sim"], res["tinc_true"], res["tinc_age"], res["branches"]
    valid = age <= TB_MAX_AGE_S
    target = np.arange(len(df)) >= WARMUP_STEPS
    branches = []
    for b, name in BRANCH_NAMES.items():
        m = (br == b) & valid & target
        e = sim[m] - truth[m]
        branches.append(dict(branch=name, samples=int(m.sum()),
                             rmse_c=float(np.sqrt(np.mean(e * e))) if m.any() else float("nan"),
                             bias_c=float(np.mean(e)) if m.any() else float("nan")))
    wins = detect_shutdown_windows(df, cold_steps=COLD_FIT_STEPS)
    items = []
    if wins["cold"]:
        items.append(("cold_start", wins["cold"]))
    for i, w in enumerate(wins["cutoffs"]):
        items.append((f"cutoff_{i}", w))
    for i, w in enumerate(wins["rundowns"]):
        items.append((f"run_down_{i}", w))
    if wins["final"]:
        items.append(("final_shutdown", wins["final"]))
    windows = []
    for name, (a, b) in items:
        m = np.zeros(len(df), dtype=bool)
        m[a:b] = True
        m &= valid
        e = sim[m] - truth[m]
        windows.append(dict(window=name, samples=int(m.sum()),
                            rmse_c=float(np.sqrt(np.mean(e * e))) if m.any() else float("nan"),
                            bias_c=float(np.mean(e)) if m.any() else float("nan")))
    finite_b = [r for r in branches if np.isfinite(r["rmse_c"])]
    finite_w = [r for r in windows if np.isfinite(r["rmse_c"]) and r["samples"]]
    return dict(
        branches=branches, windows=windows,
        worst_branch=max(finite_b, key=lambda r: r["rmse_c"])["branch"],
        worst_branch_rmse=max(r["rmse_c"] for r in finite_b),
        worst_window=max(finite_w, key=lambda r: r["rmse_c"])["window"],
        worst_window_rmse=max(r["rmse_c"] for r in finite_w),
        stale_samples=int(res["branch_counts"]["stale_hold"]),
        stale_samples_v63=int(simulate_episode(
            df, reference_params, mode="v6", reanchor_index=WARMUP_STEPS,
            stale_rule="v63")["branch_counts"]["stale_hold"]),
    )


def dhw_details(record: dict) -> dict:
    """Measured vs simulated VAL-6 entry delay and plunge depth."""
    df = record["df"]
    ww = df["ww_flag"].values >= 0.5
    # DHW *entry* is a ww rising edge; the following falling edge is the
    # plunge window scored by the DHW suite, not the entry comparison.
    rises = np.where(~ww[:-1] & ww[1:])[0] + 1
    rises = rises[rises >= WARMUP_STEPS]
    if len(rises) == 0:
        return dict(measured_delay_s=float("nan"), simulated_delay_s=float("nan"),
                    measured_depth_c=float("nan"), simulated_depth_c=float("nan"))
    i = int(rises[0])
    truth = record["tinc_true"]
    sim = record["sim"]
    end = min(len(df), i + 60)

    def delay(x):
        for k in range(i, end):
            if x[k] <= x[i] - 1.0:
                return float((k - i) * DT)
        return float((end - i) * DT)

    return dict(
        measured_delay_s=delay(truth), simulated_delay_s=delay(sim),
        measured_depth_c=float(truth[i] - np.min(truth[i:end])),
        simulated_depth_c=float(sim[i] - np.min(sim[i:end])),
    )


def main() -> None:
    print_banner(f"Phase 2 v{VERSION} - Primary Heat Exchanger Dynamics")
    print(f"Base directory:   {BASE_DIR}")
    print(f"Database:         {DB_PATH}")
    print(f"Output directory: {OUT_DIR}")
    assert DB_PATH.exists(), f"Missing DuckDB database: {DB_PATH}"

    # Phase 0 provenance: the holdout and gate files must match the freeze
    # manifest exactly, otherwise the holdout is not evaluated at all.
    assert FREEZE_PATH.exists(), f"Missing {FREEZE_PATH.name}"
    freeze = json.loads(FREEZE_PATH.read_text())
    gates = json.loads(GATES_PATH.read_text())
    for key, path in (("holdout_sha256", HOLDOUT_PATH),
                      ("gates_sha256", GATES_PATH)):
        actual = sha256_file(path)
        assert actual == freeze[key], (
            f"{path.name} hash changed after the Phase 0 freeze: "
            f"{actual} != {freeze[key]}. Refusing to evaluate the holdout.")
    print(f"Frozen gates SHA-256:   {freeze['gates_sha256']}")
    print(f"Frozen holdout SHA-256: {freeze['holdout_sha256']}")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    sensor_res = sensor_resolutions(con)

    # -- 1. FIT ------------------------------------------------------------
    print_section("1. FIT identification")
    df_fit = load_fit_1m(con)
    # Carve FIT-DHW out of the 1-24 Dec ratio window: it is a dedicated FIT
    # episode and must not enter the ratio estimator.
    dhw_a = pd.Timestamp(EPISODES["FIT-DHW"]["t_start"])
    dhw_b = pd.Timestamp(EPISODES["FIT-DHW"]["t_end"])
    fit_mask = ~((df_fit["t"] >= dhw_a) & (df_fit["t"] <= dhw_b))
    fit_ratios = identify_fit_ratios(df_fit.loc[fit_mask].reset_index(drop=True))
    hx_fit = identify_hx_fit1(con)
    dhw_fit = identify_dhw_fit(con)
    print(f"FIT ratios: B1/A={fit_ratios['ratio_B1']:.4f}, "
          f"B2/A={fit_ratios['ratio_B2']:.4f}, C/A={fit_ratios['ratio_C']:.4f}")
    print(f"FIT-1: tau_hx={hx_fit['tau_hx']:.4f} s, eps_A={hx_fit['eps_A']:.6f}, "
          f"RMSE={hx_fit['rmse']:.4f} C")
    print(f"tau_idle={fit_ratios['tau_idle']:.1f} s "
          f"(N={fit_ratios['tau_idle_stats']['segments']}), "
          f"tau_rad_off={fit_ratios['tau_rad_off']:.1f} s "
          f"(N={fit_ratios['tau_rad_off_stats']['segments']})")

    # Cold-start fit window: measured transient on TUNE only, using the frozen
    # v6.2 reference parameters when available.
    global COLD_FIT_STEPS
    ref_path = Path(__file__).resolve().parent / "step2_params_v6.json"
    ref_params = dict(V5_FROZEN)
    if ref_path.exists():
        ref_params = json.loads(ref_path.read_text())["identified_parameters"]
    COLD_FIT_STEPS, cold_durations = measure_cold_transient_steps(con, ref_params)
    print(f"Cold-start transient window: {COLD_FIT_STEPS} samples")

    params0 = base_params_from_fit(fit_ratios, hx_fit, dhw_fit)

    # -- 2. TUNE -----------------------------------------------------------
    print_section("2. TUNE: cold-start fit + coordinate search")
    cold_fit = fit_cold_start_tune(con, params0, COLD_FIT_STEPS)
    params0["L_cold_start"] = cold_fit["L_cold_start"]
    params0["tau_cold_front"] = cold_fit["tau_cold_front"]
    tune = tune_v6(con, params0, COLD_FIT_STEPS)
    params = tune["params"]
    print(f"TUNE objective = {tune['objective']:.4f} C")
    for eid, r in zip(tune["tune_ids"], tune["tune_rmse"]):
        print(f"  {eid} RMSE = {r:.4f} C")

    # -- 3. Unit tests -----------------------------------------------------
    print_section("3. Synthetic unit tests")
    unit_frames = build_unit_frames()
    unit_tests = run_unit_tests(params, unit_frames)
    for t in unit_tests:
        print(f"  {'PASS' if t['passed'] else 'FAIL'}  {t['name']:<26} {t['detail']}")
    assert all(t["passed"] for t in unit_tests), "Synthetic unit test failure"

    # -- 4. Freeze the parameters -----------------------------------------
    print_section("4. Parameter freeze (before any VAL or holdout evaluation)")
    param_blob = json.dumps({k: float(v) for k, v in sorted(params.items())},
                            separators=(",", ":"))
    param_hash = hashlib.sha256(param_blob.encode()).hexdigest()
    print(f"Final parameter dict SHA-256: {param_hash}")

    # -- 5. Observed VAL ---------------------------------------------------
    print_section("5. Observed validation (VAL-1..VAL-7; not sign-off)")
    val_records = evaluate_suite(con, params)
    for r in val_records:
        print(f"  {r['id']:<6} RMSE={r['rmse']:.3f} C  MAE={r['mae']:.3f}  "
              f"R2={r['r2']:.4f}  stale={r['branch_counts']['stale_hold']}")

    # -- 6. Holdout, exactly once -----------------------------------------
    print_section("6. Phase 0 holdout (single evaluation)")
    holdout_records = []
    for eid in HOLDOUT_IDS:
        meta = EPISODES[eid]
        df = load_episode(con, meta["t_start"], meta["t_end"])
        res = simulate_episode(df, params, mode="v6",
                               reanchor_index=WARMUP_STEPS)
        m = window_metrics(res["sim"], res["tinc_true"], WARMUP_STEPS,
                           tinc_age=res["tinc_age"])
        holdout_records.append(dict(
            id=eid, name=meta["name"], t_start=meta["t_start"],
            t_end=meta["t_end"], samples=m["samples"],
            excluded_samples=m["excluded_samples"], rmse=m["rmse"],
            mae=m["mae"], p95=m["p95"], bias=m["bias"], r2=m["r2"],
            branch_counts=res["branch_counts"], sim=res["sim"],
            tinc_true=res["tinc_true"], tinc_age=res["tinc_age"], df=df))
        print(f"  {eid}: RMSE={m['rmse']:.4f} C  R2={m['r2']:.4f}")

    sh_ids = gates["space_heating_suite"]["episodes"]
    sh = suite_scores([r for r in holdout_records if r["id"] in sh_ids],
                      lambda n, ww: np.zeros(n, dtype=bool))
    dhw_ids = gates["dhw_suite"]["episodes"]
    dhw_recs = [r for r in holdout_records if r["id"] in dhw_ids]
    dice, dtrue = [], []
    for r in dhw_recs:
        mask = dhw_plunge_mask(len(r["df"]), r["df"]["ww_flag"].values)
        if mask.any():
            dice.append(r["sim"][mask])
            dtrue.append(r["tinc_true"][mask])
    if dice:
        ds, dt_ = np.concatenate(dice), np.concatenate(dtrue)
        de = ds - dt_
        ss = float(np.sum((dt_ - np.mean(dt_)) ** 2))
        dhw_score = dict(rmse=float(np.sqrt(np.mean(de * de))),
                         samples=int(len(dt_)),
                         r2=float(1.0 - np.sum(de * de) / ss) if ss > 0 else float("nan"))
    else:
        dhw_score = dict(rmse=float("nan"), samples=0, r2=float("nan"))
    gate_eval = gate_rows(gates, sh, dhw_score)
    print(f"  Space-heating suite: mean RMSE={sh['mean_rmse']:.4f} C, "
          f"pooled RMSE={sh['pooled_rmse']:.4f} C")
    print(f"  DHW plunge: RMSE={dhw_score['rmse']:.4f} C")

    # -- 7. Five-way, parity, export, report ------------------------------
    print_section("7. Five-way TUNE comparisons")
    five_t1 = five_way(con, "TUNE-1", params)
    five_t3 = five_way(con, "TUNE-3", params)
    for five in (five_t1, five_t3):
        for label, r in five["results"].items():
            print(f"  {five['eid']} {label:<58} overall={r['overall']['rmse']:.3f} C")

    print_section("8. Python/C++ parity (g++ -O2, two parameter sets)")
    parity_episodes = {eid: load_episode(con, meta["t_start"], meta["t_end"])
                       for eid, meta in EPISODES.items()}
    parity = run_cpp_parity(OUT_DIR, parity_episodes, unit_frames, params)
    print(f"  default: {parity['default']['max_abs_diff_c']:.3e} C, "
          f"perturbed: {parity['perturbed']['max_abs_diff_c']:.3e} C")

    # -- Metrics used by the report and JSON -------------------------------
    tune_metrics = {}
    tune_dfs = {eid: load_episode(con, meta["t_start"], meta["t_end"])
                for eid, meta in EPISODES.items() if meta["kind"] == "TUNE"}
    for eid, df in tune_dfs.items():
        res = simulate_episode(df, params, mode="v6", reanchor_index=WARMUP_STEPS)
        tune_metrics[eid] = window_metrics(res["sim"], res["tinc_true"],
                                           WARMUP_STEPS, tinc_age=res["tinc_age"])

    stale_counts = {r["id"]: int(r["branch_counts"]["stale_hold"])
                    for r in val_records + holdout_records}
    for eid, df in tune_dfs.items():
        res = simulate_episode(df, params, mode="v6", reanchor_index=WARMUP_STEPS)
        stale_counts[eid] = int(res["branch_counts"]["stale_hold"])

    dhw6 = next(r for r in val_records if r["id"] == "VAL-6")
    dhw6_info = dhw_details(dhw6)

    # FIT-DHW RMSE at the final parameter dict (not part of the objective).
    fitdhw_df = load_episode(con, EPISODES["FIT-DHW"]["t_start"],
                             EPISODES["FIT-DHW"]["t_end"], warmup_min=0.0)
    wwfd = fitdhw_df["ww_flag"].values > 0.5
    efd = int(np.where(wwfd[1:] & ~wwfd[:-1])[0][0] + 1)
    res_fd = simulate_episode(fitdhw_df, params, mode="v6", reanchor_index=0)
    fit_dhw_final_rmse = float(np.sqrt(np.mean(
        (res_fd["sim"][efd:efd + 60] - res_fd["tinc_true"][efd:efd + 60]) ** 2)))

    v62_attr = val5_attribution(con, ref_params)
    disjoint = assert_disjoint()

    # -- Provenance --------------------------------------------------------
    grid_params = ("L_pump_off", "L_burn_off", "tau_burn_off", "tau_cutoff",
                   "L_cold_start", "tau_cold_front", "post_dhw_delay_s",
                   "dhw_delay_s", "tau_dhw_fast")
    moved = {h["parameter"] for h in tune["history"]
             if h["parameter"] != "initial"}
    provenance = {
        "tau_hx": "FIT-1 (shortened, input-only mask, fresh samples)",
        "eps_A": "FIT-1 (shortened, input-only mask, fresh samples)",
        "eps_B1": "FIT 1-24 Dec input-only ratio B1/A * eps_A",
        "eps_B2": "FIT 1-24 Dec input-only ratio B2/A * eps_A",
        "eps_C": "FIT 1-24 Dec input-only ratio C/A * eps_A",
        "tau_rad_off": "FIT per-segment exponential fit (median)",
        "tau_idle": "FIT per-segment exponential fit (median)",
    }
    for k in grid_params:
        x0 = float(tune["initial"][k])
        x1 = float(params[k])
        if k in ("L_cold_start", "tau_cold_front"):
            base = f"all-TUNE cold-start fit ({x0:g} s)"
            if abs(x1 - x0) > 1e-12:
                provenance[k] = f"{base}; TUNE search moved to {x1:g} s"
            else:
                provenance[k] = f"{base}; stationary on the TUNE search"
        elif abs(x1 - x0) > 1e-12:
            provenance[k] = f"TUNE grid search (moved from {x0:g} to {x1:g})"
        else:
            provenance[k] = "TUNE grid search (stationary)"

    commit = git_commit()

    # -- JSON --------------------------------------------------------------
    print_section("9. Artifact export")
    payload = {
        "metadata": {
            "version": VERSION, "phase": 2,
            "description": "Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "script": "simulation/step2_hx_dynamics_v63.py",
            "database": _relative_db_path(),
            "commit": commit,
            "phase0_commit": freeze.get("phase0_commit"),
            "gates_sha256": freeze["gates_sha256"],
            "holdout_sha256": freeze["holdout_sha256"],
            "parameter_sha256": param_hash,
        },
        "split": {eid: dict(meta) for eid, meta in EPISODES.items()},
        "disjointness": disjoint,
        "identified_parameters": params,
        "parameter_provenance": provenance,
        "fit_identification": {
            "regime_ratios": fit_ratios, "hx_fit1": hx_fit,
            "dhw_fitdhw": dhw_fit, "cold_fit_tune": cold_fit,
            "cold_transient": cold_durations,
            "fit_dhw_final_rmse_c": fit_dhw_final_rmse,
            "v62_val5_attribution": v62_attr,
        },
        "tuning": tune,
        "tune_metrics": tune_metrics,
        "validation_scenarios": val_records,
        "holdout_scenarios": holdout_records,
        "acceptance_criteria": {"gates": gate_eval,
                                "overall_pass": all(g["passed"] for g in gate_eval)},
        "five_way_tune1": five_t1["results"],
        "five_way_tune3": five_t3["results"],
        "parity": parity,
        "unit_tests": unit_tests,
        "staleness": {
            "tb_max_age_s": TB_MAX_AGE_S,
            "event_max_age_s": EVT_MAX_AGE_S,
            "policy": ("analog tb/tret <= 900 s; event bp/pmod/p1/p2/p3 stale "
                       "only if > 14400 s and tb_age > 900 s"),
            "stale_hold_counts": stale_counts,
        },
        "sensor_resolution_c": sensor_res,
        "hydraulic_decoupling": {
            "settled": fit_ratios["fit_regime_a_correlations"],
            "full_fit_window": fit_ratios["fit_full_window_correlations"],
            "interpretation": ("no material linear association of mixer_state "
                               "with dT_hx; mixer_state does correlate with "
                               "T_incoming"),
        },
    }
    payload = json_safe(payload)
    params_json = OUT_DIR / PARAMS_JSON_NAME
    params_json.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  Wrote {params_json}")

    # -- Report ------------------------------------------------------------
    report_path = OUT_DIR / REPORT_NAME
    ctx = dict(
        fit_ratios=fit_ratios, hx_fit=hx_fit, dhw_fit=dhw_fit,
        cold_fit=cold_fit, cold_fit_steps=COLD_FIT_STEPS,
        cold_durations=cold_durations,
        params=params, provenance=provenance, tune=tune,
        tune_metrics=tune_metrics, val_records=val_records,
        holdout_records=holdout_records, five_tune1=five_t1,
        five_tune3=five_t3, parity=parity, unit_tests=unit_tests,
        gates=gates, freeze=freeze, holdout_sh=sh, holdout_dhw=dhw_score,
        gate_eval=gate_eval, param_hash=param_hash, commit=commit,
        stale_counts=stale_counts, v62_attribution=v62_attr,
        dhw6_details=dhw6_info, fit_dhw_final_rmse=fit_dhw_final_rmse,
        sensor_res=sensor_res,
    )
    build_report(report_path, ctx)
    print(f"  Wrote {report_path}")

    plot_path = OUT_DIR / PLOT_NAME
    plot_tune1(plot_path, five_t1)
    print(f"  Wrote {plot_path}")

    print_banner(f"Phase 2 v{VERSION} complete")
    print(f"Holdout overall: {'PASS' if all(g['passed'] for g in gate_eval) else 'FAIL'}")
    print(f"Max Python/C++ difference: {parity['max_abs_diff_c']:.3e} C")


if __name__ == "__main__":
    main()
