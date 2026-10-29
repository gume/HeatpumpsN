#!/usr/bin/env python3
"""Step 2 v7 - Phase 0: select the untouched holdout and the FIT/TUNE day split.

Telemetry only: this script does NOT import any simulator code.  It runs
before any v7 model code is written, and its outputs are committed and hashed.

Episodes are 12-hour blocks aligned to 00:00 / 12:00 local time inside the
heating season (2025-12-02 00:00 .. 2026-05-17 24:00).

Protected (previously observed) windows: every FIT/TUNE/VAL window used by
v5..v6.3, including VAL-5 (seen in v6.2) and VAL-8/VAL-9 (seen in v6.3).
A block is a holdout candidate only if it is at least BUFFER away from every
protected window (incl. the 30-min warm-up).

Holdout criteria per block (1-minute causal grid):
  * >= 1 cold start: burner rising edge after >= 30 min with bp = 0 and
    pmod = 0, with T_incoming < 28 C at the edge;
  * >= 2 mid-run cutoffs: burner falling edge with ww = 0 whose burner
    relights within 60 min, the relight also outside DHW;
  * a final shutdown: the last burner falling edge of the block, >= 60 min
    before the block end, with no relight afterwards;
  * <= 2 % of minutes with an analog age (tb, tinc, tret) > 900 s.
  * Circulating DHW (flag): a ww rising edge with space-heating firing
    (bp > 0, ww = 0) during the preceding 10 min and a secondary pump on.

Selection: candidates are stratified by calendar month (Dec, Jan, Feb, Mar,
Apr); one block per month is drawn at random (fixed seed), and the set must
contain at least one circulating-DHW block (redraw within the seed sequence
until satisfied).  Holdout blocks get IDs HO-1..HO-n.

Day split for the remaining blocks: every block not overlapping
(holdout +/- BUFFER) or any observed VAL window is assigned to FIT if its
local calendar day ordinal is even and to TUNE if odd.  Previously used
FIT/TUNE windows are not protected from re-use in FIT/TUNE.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "common"))
import plantdata as pdx  # noqa: E402

OUT_PATH = HERE / "holdout_v7.json"
SEED = 20261029
BUFFER = pd.Timedelta(hours=6)
WARMUP = pd.Timedelta(minutes=30)
FIRST_DAY = pd.Timestamp("2025-12-02 00:00:00+01:00")
LAST_DAY_END = pd.Timestamp("2026-05-18 00:00:00+02:00")

# Every window used by any earlier Step 2 version as FIT, TUNE or VAL.
OBSERVED_VAL = {
    "VAL-1": ("2026-01-15 00:00:00+01:00", "2026-01-15 05:00:00+01:00"),
    "VAL-2": ("2026-02-19 14:40:00+01:00", "2026-02-19 17:25:00+01:00"),
    "VAL-3": ("2026-01-13 05:00:00+01:00", "2026-01-13 07:35:00+01:00"),
    "VAL-4": ("2026-01-14 05:00:00+01:00", "2026-01-14 07:30:00+01:00"),
    "VAL-5": ("2026-02-26 00:00:00+01:00", "2026-02-26 12:00:00+01:00"),
    "VAL-6": ("2026-02-18 05:50:00+01:00", "2026-02-18 06:55:00+01:00"),
    "VAL-7": ("2026-01-03 03:20:00+01:00", "2026-01-03 03:50:00+01:00"),
    "VAL-8": ("2026-03-16 00:00:00+01:00", "2026-03-16 12:00:00+01:00"),
    "VAL-9": ("2026-03-22 12:00:00+01:00", "2026-03-23 00:00:00+01:00"),
}
OLD_FIT_TUNE = {
    "FIT_WINDOW": ("2025-12-01 22:00:00+01:00", "2025-12-24 23:59:00+01:00"),
    "FIT-1": ("2026-01-14 00:00:00+01:00", "2026-01-14 05:00:00+01:00"),
    "FIT-DHW": ("2025-12-03 08:18:00+01:00", "2025-12-03 08:45:00+01:00"),
    "TUNE-1": ("2025-12-27 16:06:00+01:00", "2025-12-27 20:58:00+01:00"),
    "TUNE-2": ("2026-01-01 14:50:00+01:00", "2026-01-01 15:25:00+01:00"),
    "TUNE-3": ("2026-02-08 04:27:00+01:00", "2026-02-08 10:11:00+01:00"),
}

CRITERIA = dict(block_hours=12, buffer_hours=6, warmup_minutes=30,
                cold_idle_minutes=30, cold_tinc_max_c=28.0,
                relight_within_minutes=60, min_mid_run_cutoffs=2,
                final_shutdown_min_before_end_minutes=60,
                max_stale_fraction=0.02, analog_max_age_s=900.0,
                dhw_preceding_firing_minutes=10,
                stratify="one block per calendar month Dec..Apr",
                require_circulating_dhw_in_set=True, seed=SEED)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_blob(path: Path) -> str:
    return subprocess.check_output(["git", "hash-object", str(path)],
                                   cwd=HERE, text=True).strip()


def blocks() -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    day = FIRST_DAY
    while day < LAST_DAY_END:
        d0 = pd.Timestamp(day.strftime("%Y-%m-%d"), tz="Europe/Budapest")
        for h in (0, 12):
            a = d0 + pd.Timedelta(hours=h)
            b = d0 + pd.Timedelta(hours=h + 12)
            if b <= LAST_DAY_END:
                out.append((a, b))
        day = day + pd.Timedelta(days=1)
    return out


def overlaps(a, b, wins, pad) -> bool:
    for s, e in wins:
        if a < pd.Timestamp(e) + pad and b > pd.Timestamp(s) - WARMUP - pad:
            return True
    return False


def analyse(x: pd.DataFrame) -> dict:
    """Event detection on a 1-minute frame of one block."""
    bp = x["bp"].fillna(0).values > 0
    pm = x["pmod"].fillna(0).values > 0
    ww = x["ww"].fillna(0).values >= 0.5
    tinc = x["tinc"].values
    pump = ((x["p1w"].fillna(0).values >= pdx.PUMP_ON_W)
            | (x["p2w"].fillna(0).values >= pdx.PUMP_ON_W)
            | (x["p3w"].fillna(0).values >= pdx.PUMP_ON_W))
    n = len(x)
    rises = [i for i in range(1, n) if bp[i] and not bp[i - 1]]
    falls = [i for i in range(1, n) if (not bp[i]) and bp[i - 1]]
    cold = 0
    ci = CRITERIA["cold_idle_minutes"]
    for i in rises:
        if i >= ci and not bp[i - ci:i].any() and not pm[i - ci:i].any() \
                and tinc[i] < CRITERIA["cold_tinc_max_c"]:
            cold += 1
    cut = 0
    rw = CRITERIA["relight_within_minutes"]
    for i in falls:
        if ww[i] or ww[i - 1]:
            continue
        nxt = [r for r in rises if i < r <= i + rw and not ww[r]]
        if nxt:
            cut += 1
    final = False
    if falls:
        last = falls[-1]
        if not any(r > last for r in rises) and \
                last <= n - CRITERIA["final_shutdown_min_before_end_minutes"]:
            final = True
    ages = np.c_[x["tb_age"].values, x["tinc_age"].values, x["tret_age"].values]
    stale = float(np.mean(~(np.nan_to_num(ages, nan=1e9) <= CRITERIA["analog_max_age_s"]).all(axis=1)))
    dhw = 0
    pf = CRITERIA["dhw_preceding_firing_minutes"]
    for i in range(1, n):
        if ww[i] and not ww[i - 1] and i >= pf:
            if (bp[i - pf:i] & ~ww[i - pf:i]).any() and pump[i]:
                dhw += 1
    return dict(cold_starts=cold, mid_run_cutoffs=cut, final_shutdown=final,
                stale_fraction=stale, circulating_dhw=dhw)


def main() -> None:
    con = pdx.connect()
    grid = pdx.load_grid(str(FIRST_DAY - pd.Timedelta(hours=1)),
                         str(LAST_DAY_END), 60, con)
    grid = grid.set_index("t")
    obs = list(OBSERVED_VAL.values())
    cands = []
    for a, b in blocks():
        if overlaps(a, b, obs, BUFFER):
            continue
        x = grid.loc[(grid.index >= a) & (grid.index < b)]
        r = analyse(x)
        ok = (r["cold_starts"] >= 1 and r["mid_run_cutoffs"] >= CRITERIA["min_mid_run_cutoffs"]
              and r["final_shutdown"] and r["stale_fraction"] <= CRITERIA["max_stale_fraction"])
        if ok:
            cands.append(dict(start=a, end=b, month=a.strftime("%Y-%m"), **r))
    print(f"qualifying candidate blocks: {len(cands)}")
    months = sorted({c["month"] for c in cands})
    months = [m for m in months if m[5:7] in ("12", "01", "02", "03", "04")]
    rng = np.random.default_rng(SEED)
    chosen = None
    for attempt in range(1000):
        pick = []
        for m in months:
            pool = [c for c in cands if c["month"] == m]
            pick.append(pool[int(rng.integers(len(pool)))])
        if any(c["circulating_dhw"] > 0 for c in pick):
            chosen = (attempt, pick)
            break
    assert chosen is not None, "no holdout set with circulating DHW"
    attempt, pick = chosen
    holdout = {}
    for i, c in enumerate(sorted(pick, key=lambda c: c["start"]), 1):
        holdout[f"HO-{i}"] = dict(
            t_start=c["start"].isoformat(), t_end=c["end"].isoformat(),
            cold_starts=c["cold_starts"], mid_run_cutoffs=c["mid_run_cutoffs"],
            final_shutdown=c["final_shutdown"],
            stale_fraction=round(c["stale_fraction"], 5),
            circulating_dhw=c["circulating_dhw"])
    ho_wins = [(v["t_start"], v["t_end"]) for v in holdout.values()]

    split = {"FIT": [], "TUNE": [], "EXCLUDED": []}
    for a, b in blocks():
        if overlaps(a, b, obs, pd.Timedelta(0)) or overlaps(a, b, ho_wins, BUFFER):
            split["EXCLUDED"].append(a.isoformat())
            continue
        key = "FIT" if a.toordinal() % 2 == 0 else "TUNE"
        split[key].append(a.isoformat())

    payload = dict(
        version="v7", created_utc=datetime.now(timezone.utc).isoformat(),
        selector_script="simulation/step2/select_holdout_v7.py",
        selector_git_blob=git_blob(Path(__file__)),
        selector_sha256=sha256(Path(__file__)),
        criteria=CRITERIA, observed_val=OBSERVED_VAL, old_fit_tune=OLD_FIT_TUNE,
        n_candidates=len(cands), draw_attempt=attempt,
        holdout=holdout,
        split_rule="12 h blocks; EXCLUDED = overlaps an observed VAL window or "
                   "holdout +/- 6 h; FIT = even local day ordinal, TUNE = odd",
        split=split,
    )
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(json.dumps(holdout, indent=2))
    print({k: len(v) for k, v in split.items()})
    print("sha256", sha256(OUT_PATH))


if __name__ == "__main__":
    main()
