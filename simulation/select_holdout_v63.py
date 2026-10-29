#!/usr/bin/env python3
"""Select untouched v6.3 holdout episodes from telemetry only.

This script deliberately does NOT import the HX simulator.  It scans 12-hour
candidate windows at a 1-minute grid and applies the Phase 0 criteria from
``step2_v6.3_fix_instructions.md``:

* outside every FIT/TUNE/VAL window, their 30-minute warm-ups, and a 6-hour
  buffer around them;
* at least one cold start (30 min with no burner and no primary pump, followed
  by a firing with manifold temperature below 28 C);
* at least two genuine mid-run burner cutoffs (burner relights afterwards);
* a final shutdown (last burner falling edge, no relight, pumps off at end);
* at most 2 % of samples with any input age over its effective threshold;
* preferably at least one window containing circulating DHW.

The effective staleness rule used for selection mirrors the intended v6.3
event-driven rule: analog ages (tb/tinc/tret) are limited by 900 s; event ages
(burner, pump modulation, pump powers) are considered stale only when they
exceed 14400 s *and* the boiler-temperature heartbeat is also stale.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "analysis" / "heatpumps.duckdb"
OUT_PATH = Path(__file__).resolve().parent / "holdout_v63.json"

TB_MAX_AGE_S = 900.0
EVT_MAX_AGE_S = 14400.0
WARMUP = pd.Timedelta(minutes=30)
GAP = pd.Timedelta(hours=6)
COLD_IDLE = pd.Timedelta(minutes=30)
COLD_TEMP_C = 28.0

# Every episode and identification window known before Phase 0.  FIT-1 is
# included at its v6.2 length (00:00-05:00); Phase 2 shortens it, which only
# makes the protected region smaller and therefore remains safe.
PROTECTED = {
    "FIT_WINDOW": ("2025-12-01 22:00:00+01:00", "2025-12-24 23:59:00+01:00"),
    "FIT-1": ("2026-01-14 00:00:00+01:00", "2026-01-14 05:00:00+01:00"),
    "FIT-DHW": ("2025-12-03 08:18:00+01:00", "2025-12-03 08:45:00+01:00"),
    "TUNE-1": ("2025-12-27 16:06:00+01:00", "2025-12-27 20:58:00+01:00"),
    "TUNE-2": ("2026-01-01 14:50:00+01:00", "2026-01-01 15:25:00+01:00"),
    "TUNE-3": ("2026-02-08 04:27:00+01:00", "2026-02-08 10:11:00+01:00"),
    "VAL-1": ("2026-01-15 00:00:00+01:00", "2026-01-15 05:00:00+01:00"),
    "VAL-2": ("2026-02-19 14:40:00+01:00", "2026-02-19 17:25:00+01:00"),
    "VAL-3": ("2026-01-13 05:00:00+01:00", "2026-01-13 07:35:00+01:00"),
    "VAL-4": ("2026-01-14 05:00:00+01:00", "2026-01-14 07:30:00+01:00"),
    "VAL-5": ("2026-02-26 00:00:00+01:00", "2026-02-26 12:00:00+01:00"),
    "VAL-6": ("2026-02-18 05:50:00+01:00", "2026-02-18 06:55:00+01:00"),
    "VAL-7": ("2026-01-03 03:20:00+01:00", "2026-01-03 03:50:00+01:00"),
}


def git_blob_hash(path: Path) -> str:
    return subprocess.check_output(
        ["git", "hash-object", str(path)], cwd=BASE_DIR, text=True
    ).strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def protected_intervals() -> list[tuple[pd.Timestamp, pd.Timestamp, str]]:
    out = []
    for name, (start, end) in PROTECTED.items():
        a = pd.Timestamp(start) - WARMUP
        b = pd.Timestamp(end) + WARMUP
        out.append((a, b, name))
    return out


def overlaps_protected(start: pd.Timestamp, end: pd.Timestamp,
                       intervals) -> bool:
    for a, b, _ in intervals:
        if not (end + GAP <= a or start - GAP >= b):
            return True
    return False


def load_candidate(con, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    q = f"""
    WITH grid AS (
        SELECT unnest(generate_series(
            TIMESTAMPTZ '{start.isoformat()}',
            TIMESTAMPTZ '{end.isoformat()}',
            INTERVAL 1 MINUTE
        )) AS t
    )
    SELECT g.t,
        coalesce(bp.v, 0.0)   AS burner_pct,
        coalesce(pmod.v, 0.0) AS pump_mod,
        coalesce(p1.v, 0.0)   AS p1_w,
        coalesce(p2.v, 0.0)   AS p2_w,
        coalesce(p3.v, 0.0)   AS p3_w,
        coalesce(ww.v, 0.0)   AS ww_flag,
        tb.v AS t_boiler,
        ti.v AS t_inc,
        tr.v AS t_ret,
        (epoch(g.t) - epoch(tb.t))   AS tb_age_s,
        (epoch(g.t) - epoch(ti.t))   AS tinc_age_s,
        (epoch(g.t) - epoch(tr.t))   AS tret_age_s,
        (epoch(g.t) - epoch(bp.t))   AS bp_age_s,
        (epoch(g.t) - epoch(pmod.t)) AS pmod_age_s,
        (epoch(g.t) - epoch(p1.t))   AS p1_age_s,
        (epoch(g.t) - epoch(p2.t))   AS p2_age_s,
        (epoch(g.t) - epoch(p3.t))   AS p3_age_s
    FROM grid g
    ASOF JOIN boiler_curburnpow bp           ON g.t >= bp.t
    ASOF JOIN boiler_heatingpumpmod pmod     ON g.t >= pmod.t
    ASOF JOIN heatpumps_pump_1_power p1      ON g.t >= p1.t
    ASOF JOIN heatpumps_pump_2_power p2      ON g.t >= p2.t
    ASOF JOIN heatpumps_pump_3_power p3      ON g.t >= p3.t
    ASOF JOIN boiler_wwcharging ww           ON g.t >= ww.t
    ASOF JOIN boiler_curflowtemp tb          ON g.t >= tb.t
    ASOF JOIN mixer_incoming_temperature ti  ON g.t >= ti.t
    ASOF JOIN mixer_return_temperature tr    ON g.t >= tr.t
    ORDER BY g.t
    """
    df = con.execute(q).df()
    assert not df.isna().any().any(), "NaN in candidate telemetry"
    return df


SCAN_START = "2026-03-01"
SCAN_END = "2026-04-14"


def load_range(con, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Load one 1-minute telemetry frame covering every candidate window.

    The whole scan range is loaded once; per-window features are then computed
    on slices.  This is equivalent to querying each window separately but keeps
    the selector's runtime bounded.
    """
    q = f"""
    WITH grid AS (
        SELECT unnest(generate_series(
            TIMESTAMPTZ '{start.isoformat()}',
            TIMESTAMPTZ '{end.isoformat()}',
            INTERVAL 1 MINUTE
        )) AS t
    )
    SELECT g.t,
        coalesce(bp.v, 0.0)   AS burner_pct,
        coalesce(pmod.v, 0.0) AS pump_mod,
        coalesce(p1.v, 0.0)   AS p1_w,
        coalesce(p2.v, 0.0)   AS p2_w,
        coalesce(p3.v, 0.0)   AS p3_w,
        coalesce(ww.v, 0.0)   AS ww_flag,
        tb.v AS t_boiler,
        ti.v AS t_inc,
        tr.v AS t_ret,
        (epoch(g.t) - epoch(tb.t))   AS tb_age_s,
        (epoch(g.t) - epoch(ti.t))   AS tinc_age_s,
        (epoch(g.t) - epoch(tr.t))   AS tret_age_s,
        (epoch(g.t) - epoch(bp.t))   AS bp_age_s,
        (epoch(g.t) - epoch(pmod.t)) AS pmod_age_s,
        (epoch(g.t) - epoch(p1.t))   AS p1_age_s,
        (epoch(g.t) - epoch(p2.t))   AS p2_age_s,
        (epoch(g.t) - epoch(p3.t))   AS p3_age_s
    FROM grid g
    ASOF JOIN boiler_curburnpow bp           ON g.t >= bp.t
    ASOF JOIN boiler_heatingpumpmod pmod     ON g.t >= pmod.t
    ASOF JOIN heatpumps_pump_1_power p1      ON g.t >= p1.t
    ASOF JOIN heatpumps_pump_2_power p2      ON g.t >= p2.t
    ASOF JOIN heatpumps_pump_3_power p3      ON g.t >= p3.t
    ASOF JOIN boiler_wwcharging ww           ON g.t >= ww.t
    ASOF JOIN boiler_curflowtemp tb          ON g.t >= tb.t
    ASOF JOIN mixer_incoming_temperature ti  ON g.t >= ti.t
    ASOF JOIN mixer_return_temperature tr    ON g.t >= tr.t
    ORDER BY g.t
    """
    df = con.execute(q).df()
    assert not df.isna().any().any(), "NaN in bulk telemetry load"
    return df


def features(df: pd.DataFrame) -> dict:
    bp = (df["burner_pct"].values > 0.0).astype(int)
    pmod = (df["pump_mod"].values > 0.0).astype(int)
    ww = (df["ww_flag"].values >= 0.5).astype(int)
    dt = 60.0
    back = int(round(COLD_IDLE.total_seconds() / dt))
    recover = int(round(30 * 60 / dt))

    cold = []
    for i in range(1, len(df)):
        if (bp[i] and not bp[i - 1] and i >= back
                and bp[i - back:i].sum() == 0
                and pmod[i - back:i].sum() == 0
                and float(df["t_inc"].iloc[i]) < COLD_TEMP_C):
            cold.append(i)

    cutoffs = []
    for i in range(1, len(df)):
        if bp[i - 1] and not bp[i] and np.where(bp[i:] > 0)[0].size:
            if pmod[i:i + recover].sum() > 0:
                cutoffs.append(i)

    falls = np.where((bp[:-1] > 0) & (bp[1:] == 0))[0] + 1
    final = None
    if len(falls):
        i = int(falls[-1])
        if bp[i:].sum() == 0 and pmod[-recover:].sum() == 0:
            final = i

    analog_stale = (
        (df["tb_age_s"].values > TB_MAX_AGE_S)
        | (df["tinc_age_s"].values > TB_MAX_AGE_S)
        | (df["tret_age_s"].values > TB_MAX_AGE_S)
    )
    heartbeat_stale = df["tb_age_s"].values > TB_MAX_AGE_S
    event_ages = np.vstack([
        df["bp_age_s"].values,
        df["pmod_age_s"].values,
        df["p1_age_s"].values,
        df["p2_age_s"].values,
        df["p3_age_s"].values,
    ])
    event_stale = ((event_ages > EVT_MAX_AGE_S).any(axis=0)
                   & heartbeat_stale)
    stale_fraction = float((analog_stale | event_stale).mean())

    return dict(
        cold_starts=len(cold),
        cold_indices=cold,
        cutoffs=len(cutoffs),
        cutoff_indices=cutoffs,
        final_shutdown_index=final,
        stale_fraction=stale_fraction,
        burner_minutes=int(bp.sum() * dt / 60),
        pump_minutes=int(pmod.sum() * dt / 60),
        dhw_minutes=int(ww.sum() * dt / 60),
        tinc_min_c=float(df["t_inc"].min()),
        tinc_max_c=float(df["t_inc"].max()),
    )


def qualifies(f: dict) -> bool:
    return (f["cold_starts"] >= 1
            and f["cutoffs"] >= 2
            and f["final_shutdown_index"] is not None
            and f["stale_fraction"] <= 0.02
            and f["burner_minutes"] >= 60)


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    intervals = protected_intervals()

    scan_start = pd.Timestamp(f"{SCAN_START} 00:00:00", tz="Europe/Vienna")
    scan_end = pd.Timestamp(f"{SCAN_END} 23:59:00", tz="Europe/Vienna")
    bulk = load_range(con, scan_start - COLD_IDLE, scan_end)
    bulk_index = bulk.set_index("t")

    candidates = []
    for day in pd.date_range(SCAN_START, SCAN_END, freq="D"):
        for hour in (0, 12):
            start = pd.Timestamp(f"{day.date()} {hour:02d}:00:00",
                                 tz="Europe/Vienna")
            end = start + pd.Timedelta(hours=12)
            if end > scan_end:
                continue
            if overlaps_protected(start, end, intervals):
                continue
            pre = start - COLD_IDLE
            df = bulk_index.loc[pre:end].reset_index()
            f = features(df)
            if qualifies(f):
                candidates.append(dict(start=start, end=end, features=f))

    assert len(candidates) >= 2, (
        f"Only {len(candidates)} qualifying holdout candidates found in "
        f"{SCAN_START}..{SCAN_END}; the frozen criteria cannot be satisfied."
    )

    # Prefer windows with circulating DHW, then lower stale fraction, then
    # more cold starts / cutoffs, then earlier start.  Deterministic.
    candidates.sort(key=lambda c: (
        -int(c["features"]["dhw_minutes"] >= 10),
        c["features"]["stale_fraction"],
        -c["features"]["cold_starts"],
        -c["features"]["cutoffs"],
        c["start"],
    ))
    chosen = candidates[:2]

    episodes = {}
    for i, c in enumerate(chosen, start=8):
        eid = f"VAL-{i}"
        episodes[eid] = dict(
            name=(f"{eid} Untouched Holdout: Cold Start, Cutoffs & Final Shutdown"
                  + (" + Circulating DHW" if c["features"]["dhw_minutes"] >= 10
                     else "")),
            t_start=c["start"].isoformat(),
            t_end=c["end"].isoformat(),
            kind="HOLDOUT",
            role="untouched v6.3 holdout, selected from telemetry only",
            selection_evidence=c["features"],
        )

    payload = dict(
        version="v6.3",
        created_utc=pd.Timestamp.utcnow().isoformat(),
        selector_script="simulation/select_holdout_v63.py",
        selector_git_blob=git_blob_hash(Path(__file__)),
        selector_sha256=sha256(Path(__file__)),
        scan_window=dict(start=SCAN_START, end=SCAN_END,
                         step_hours=12, grid_minutes=1,
                         note="late heating season; whole range loaded once"),
        criteria=dict(
            outside_all_fit_tune_val=True,
            warmup_minutes=30,
            buffer_hours=6,
            min_cold_starts=1,
            cold_idle_minutes=30,
            cold_manifold_max_c=COLD_TEMP_C,
            min_mid_run_cutoffs=2,
            requires_final_shutdown=True,
            max_stale_fraction=0.02,
            stale_rule=("analog tb/tinc/tret <= 900 s; event bp/pmod/p1/p2/p3 "
                        "stale only if > 14400 s and tb_age > 900 s"),
            prefer_circulating_dhw=True,
        ),
        protected_intervals=[
            dict(name=n, start=a.isoformat(), end=b.isoformat())
            for a, b, n in intervals
        ],
        candidates_considered=len(candidates),
        episodes=episodes,
    )
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {OUT_PATH}")
    for eid, ep in episodes.items():
        ev = ep["selection_evidence"]
        print(f"  {eid}: {ep['t_start']} -> {ep['t_end']}")
        print(f"        cold={ev['cold_starts']} cutoffs={ev['cutoffs']} "
              f"final={ev['final_shutdown_index']} "
              f"stale={ev['stale_fraction']:.4f} "
              f"dhw_min={ev['dhw_minutes']} "
              f"tinc={ev['tinc_min_c']:.1f}..{ev['tinc_max_c']:.1f} C")


if __name__ == "__main__":
    main()
