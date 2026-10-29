#!/usr/bin/env python3
"""Phase 1 diagnostics for the v6.3 audit fixes.

This script is deliberately read-only with respect to model code: it imports
the frozen v6.2 pipeline and its frozen v6.2 parameters, and it does **not**
touch the Phase 0 holdout episodes.  Every output is written to
``simulation/diag_v63/``.

Audit items covered
-------------------
1.1  VAL-5 per-branch / per-window RMSE, bias and sample count + overlay plot.
1.2  Stale-hold cause per violating age, and raw pump reporting intervals.
1.3  Sensor quantisation and unchanged 1-minute step shares.
1.4  Split overlap, including warm-ups (reports the FIT-1 / VAL-4 overlap).
1.5  FIT-DHW RMSE at ``dhw_delay_s`` = 135 s and 90 s.
1.6  TUNE-1 run-down vs mid-run cutoff classification of the late window.
1.7  Cold-start transient length for v5 and v6.2 on TUNE-1 and VAL-5.
1.8  Cutoff-dip mechanism with ``pump_mod`` as a value.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "diag_v63"
OUT.mkdir(parents=True, exist_ok=True)
DB = BASE / "analysis" / "heatpumps.duckdb"

spec = importlib.util.spec_from_file_location(
    "hx_v6", Path(__file__).resolve().parent / "step2_hx_dynamics_v6.py")
v6 = importlib.util.module_from_spec(spec)
sys.modules["hx_v6"] = v6
spec.loader.exec_module(v6)


def load_v62_params() -> dict:
    payload = json.loads((Path(__file__).resolve().parent /
                          "step2_params_v6.json").read_text())
    return dict(payload["identified_parameters"])


PARAMS = load_v62_params()
DT = v6.DT
WARM = v6.WARMUP_STEPS
SUMMARY: dict = {}


def write_summary() -> None:
    (OUT / "summary.json").write_text(json.dumps(SUMMARY, indent=2) + "\n")


# ---------------------------------------------------------------------------
# 1.1 VAL-5 branch / window breakdown
# ---------------------------------------------------------------------------
def diag_val5(con) -> None:
    meta = v6.EPISODES["VAL-5"]
    df = v6.load_episode(con, meta["t_start"], meta["t_end"])
    res = v6.simulate_episode(df, PARAMS, mode="v6", reanchor_index=WARM)
    sim, truth, age, br = res["sim"], res["tinc_true"], res["tinc_age"], res["branches"]
    valid = (age <= v6.TB_MAX_AGE_S)
    target = np.arange(len(df)) >= WARM
    rows = []
    for b, name in v6.BRANCH_NAMES.items():
        m = (br == b) & valid & target
        if m.sum() == 0:
            rows.append(dict(branch=name, samples=0, rmse_c=float("nan"),
                             bias_c=float("nan"), mae_c=float("nan")))
            continue
        e = sim[m] - truth[m]
        rows.append(dict(branch=name, samples=int(m.sum()),
                         rmse_c=float(np.sqrt(np.mean(e * e))),
                         bias_c=float(np.mean(e)),
                         mae_c=float(np.mean(np.abs(e)))))
    pd.DataFrame(rows).to_csv(OUT / "val5_branch_metrics.csv", index=False)

    wins = v6.detect_shutdown_windows(df)
    windows = []
    if wins["cold"]:
        windows.append(("cold_start", wins["cold"]))
    for i, w in enumerate(wins["cutoffs"]):
        windows.append((f"cutoff_{i}", w))
    if wins["final"]:
        windows.append(("final_shutdown", wins["final"]))
    wrows = []
    for name, (a, b) in windows:
        m = np.zeros(len(df), dtype=bool)
        m[a:b] = True
        m &= valid
        e = sim[m] - truth[m]
        wrows.append(dict(window=name, start=int(a), stop=int(b),
                          samples=int(m.sum()),
                          rmse_c=float(np.sqrt(np.mean(e * e))) if m.any() else float("nan"),
                          bias_c=float(np.mean(e)) if m.any() else float("nan")))
    pd.DataFrame(wrows).to_csv(OUT / "val5_window_metrics.csv", index=False)
    SUMMARY["val5_branch_metrics"] = rows
    SUMMARY["val5_window_metrics"] = wrows
    SUMMARY["val5_overall_rmse_c"] = float(
        np.sqrt(np.mean((sim[target & valid] - truth[target & valid]) ** 2)))

    # Overlay plot.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = np.arange(len(df)) * DT / 60.0
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [2.4, 1.2]})
    ax1.plot(t, df["t_boiler"], color="crimson", lw=1.3, label="T_boiler")
    ax1.plot(t, truth, color="black", lw=1.8, label="Measured T_incoming")
    ax1.plot(t, sim, color="dodgerblue", lw=1.6, ls="--", label="v6.2 sim")
    ax1.plot(t, df["t_ret"], color="forestgreen", lw=1.0, alpha=0.7,
             label="T_return")
    for name, (a, b) in windows:
        ax1.axvspan(a * DT / 60, b * DT / 60, color="orange", alpha=0.15)
    ax1.axvline(WARM * DT / 60, color="k", lw=0.8, ls="-.")
    ax1.set_ylabel("Temperature (C)")
    ax1.set_title("VAL-5 observed holdout (seen in v6.2): branch/window diagnostic")
    ax1.grid(True, ls=":", alpha=0.6)
    ax1.legend(loc="upper left", ncol=2, fontsize=9)
    ax2.plot(t, sim - truth, color="darkorchid", lw=1.3)
    ax2.axhline(0, color="black", lw=0.8)
    for name, (a, b) in windows:
        ax2.axvspan(a * DT / 60, b * DT / 60, color="orange", alpha=0.15)
    ax2.set_ylabel("Error (C)")
    ax2.set_xlabel("Elapsed minutes from warm-up start")
    ax2.grid(True, ls=":", alpha=0.6)
    fig.tight_layout()
    fig.savefig(OUT / "val5_branch_overlay.png", dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 1.2 Stale-hold cause
# ---------------------------------------------------------------------------
def diag_stale(con) -> None:
    rows = []
    for eid, meta in v6.EPISODES.items():
        df = v6.load_episode(con, meta["t_start"], meta["t_end"])
        res = v6.simulate_episode(df, PARAMS, mode="v6", reanchor_index=WARM)
        stale = res["branches"] == v6.BR_STALE
        if not stale.any():
            rows.append(dict(episode=eid, stale_samples=0, tb=0, tret=0, p3=0,
                             bp=0, pmod=0, most_common_cause="none"))
            continue
        tb = df["tb_age_s"].values > v6.TB_MAX_AGE_S
        tret = df["tret_age_s"].values > v6.TB_MAX_AGE_S
        p3 = df["p3_age_s"].values > v6.TB_MAX_AGE_S
        bp = df["bp_age_s"].values > v6.EVT_MAX_AGE_S
        pm = df["pmod_age_s"].values > v6.EVT_MAX_AGE_S
        causes = {"tb": tb, "tret": tret, "p3": p3, "bp": bp, "pmod": pm}
        counts = {k: int((v & stale).sum()) for k, v in causes.items()}
        top = max(counts, key=counts.get)
        rows.append(dict(episode=eid, stale_samples=int(stale.sum()), **counts,
                         most_common_cause=top,
                         stale_start=str(df["t"].iloc[int(np.argmax(stale))])))
        # Per-sample cause table for the largest offender only.
        if eid == "VAL-5":
            pd.DataFrame({
                "t": df["t"].values[stale],
                "tb_age_s": df["tb_age_s"].values[stale],
                "tret_age_s": df["tret_age_s"].values[stale],
                "p3_age_s": df["p3_age_s"].values[stale],
                "bp_age_s": df["bp_age_s"].values[stale],
                "pmod_age_s": df["pmod_age_s"].values[stale],
                "tinc_age_s": df["tinc_age_s"].values[stale],
            }).to_csv(OUT / "val5_stale_samples.csv", index=False)
    pd.DataFrame(rows).to_csv(OUT / "stale_cause_per_episode.csv", index=False)
    SUMMARY["stale_cause_per_episode"] = rows

    # Raw pump reporting intervals, split by on/off.
    tables = {
        "p1": ("heatpumps_pump_1_power", 2.0),
        "p2": ("heatpumps_pump_2_power", 2.0),
        "p3": ("heatpumps_pump_3_power", 2.0),
        "pmod": ("boiler_heatingpumpmod", 0.0),
        "bp": ("boiler_curburnpow", 0.0),
    }
    raw_rows = []
    for name, (table, thr) in tables.items():
        d = con.execute(f"SELECT t, v FROM {table} ORDER BY t").df()
        dt = d["t"].diff().dt.total_seconds().values[1:]
        v = d["v"].values[1:]
        for state, mask in (("on", v > thr), ("off", v <= thr)):
            x = dt[mask]
            if len(x) == 0:
                continue
            raw_rows.append(dict(
                signal=name, state=state, samples=int(len(x)),
                p50_s=float(np.percentile(x, 50)),
                p90_s=float(np.percentile(x, 90)),
                p99_s=float(np.percentile(x, 99)),
                max_s=float(np.max(x)),
                share_gt_900=float(np.mean(x > 900.0)),
                share_gt_14400=float(np.mean(x > 14400.0)),
            ))
    pd.DataFrame(raw_rows).to_csv(OUT / "raw_reporting_intervals.csv",
                                  index=False)
    SUMMARY["raw_reporting_intervals"] = raw_rows


# ---------------------------------------------------------------------------
# 1.3 Sensor quantisation
# ---------------------------------------------------------------------------
def diag_quantisation(con) -> None:
    sensors = {
        "mixer_incoming_temperature": "mixer_incoming_temperature",
        "mixer_return_temperature": "mixer_return_temperature",
        "room_temperature": "temperature_humidity_sensor_d281_temperature",
        "boiler_flow_temperature": "boiler_curflowtemp",
    }
    rows = []
    for name, table in sensors.items():
        d = con.execute(f"SELECT t, v FROM {table} ORDER BY t").df()
        vals = d["v"].values.astype(float)
        diffs = np.abs(np.diff(vals))
        pos = diffs[diffs > 1e-12]
        resolution = float(np.min(pos)) if len(pos) else float("nan")
        # 1-minute resampled unchanged steps.
        s = pd.Series(vals, index=pd.to_datetime(d["t"]))
        r = s.resample("1min").last().dropna()
        d1 = np.diff(r.values)
        idle_share = float(np.mean(np.abs(d1) < 1e-12)) if len(d1) else float("nan")
        rows.append(dict(sensor=name, raw_rows=int(len(vals)),
                         resolution_c=resolution,
                         unchanged_1min_share=idle_share,
                         raw_unchanged_share=float(np.mean(diffs < 1e-12))))
    pd.DataFrame(rows).to_csv(OUT / "sensor_quantisation.csv", index=False)
    SUMMARY["sensor_quantisation"] = rows


# ---------------------------------------------------------------------------
# 1.4 Split overlap including warm-ups
# ---------------------------------------------------------------------------
def diag_split_overlap() -> None:
    warm = pd.Timedelta(minutes=v6.WARMUP_MIN)
    spans = []
    for eid, meta in v6.EPISODES.items():
        spans.append(dict(name=eid, start=pd.Timestamp(meta["t_start"]),
                          end=pd.Timestamp(meta["t_end"])))
    spans.append(dict(name="FIT_WINDOW",
                      start=pd.Timestamp(v6.FIT_WINDOW["t_start"]),
                      end=pd.Timestamp(v6.FIT_WINDOW["t_end"])))
    rows = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a, b = spans[i], spans[j]
            a0, a1 = a["start"] - warm, a["end"] + warm
            b0, b1 = b["start"] - warm, b["end"] + warm
            if a0 < b1 and b0 < a1:
                gap_s = (max(a0, b0) - min(a1, b1)).total_seconds()
                rows.append(dict(first=a["name"], second=b["name"],
                                 overlap=1,
                                 warmup_overlap=1,
                                 overlap_span_s=-gap_s))
    for r in rows:
        print(f"  OVERLAP: {r['first']} <-> {r['second']} "
              f"({r['overlap_span_s'] / 60.0:.0f} min)")
    pd.DataFrame(rows, columns=["first", "second", "overlap", "warmup_overlap",
                                "overlap_span_s"]).to_csv(
        OUT / "split_overlap_pre_fix.csv", index=False)
    SUMMARY["split_overlap_pre_fix"] = rows


# ---------------------------------------------------------------------------
# 1.5 FIT-DHW side effect
# ---------------------------------------------------------------------------
def diag_fit_dhw(con) -> None:
    df = v6.load_episode(con, v6.EPISODES["FIT-DHW"]["t_start"],
                         v6.EPISODES["FIT-DHW"]["t_end"], warmup_min=0.0)
    ww = df["ww_flag"].values > 0.5
    edge = int(np.where(ww[1:] & ~ww[:-1])[0][0] + 1)
    truth = df["t_inc"].values
    rows = []
    for delay in (135.0, 90.0):
        p = dict(PARAMS)
        p["dhw_delay_s"] = delay
        res = v6.simulate_episode(df, p, mode="v6", reanchor_index=0)
        sim = res["sim"]
        stop = min(len(df), edge + 60)
        e = sim[edge:stop] - truth[edge:stop]
        rows.append(dict(dhw_delay_s=delay,
                         rmse_c=float(np.sqrt(np.mean(e * e))),
                         bias_c=float(np.mean(e)),
                         samples=int(stop - edge)))
    pd.DataFrame(rows).to_csv(OUT / "fit_dhw_delay_side_effect.csv", index=False)
    SUMMARY["fit_dhw_delay_side_effect"] = rows


# ---------------------------------------------------------------------------
# 1.6 TUNE-1 late run-down
# ---------------------------------------------------------------------------
def diag_tune1_rundown(con) -> None:
    df = v6.load_episode(con, v6.EPISODES["TUNE-1"]["t_start"],
                         v6.EPISODES["TUNE-1"]["t_end"])
    a, b = 1820, 1896
    bp = df["burner_pct"].values
    pm = df["pump_mod"].values
    seg = slice(a, min(b, len(df)))
    relight = bool(np.any(bp[seg] > 0.0))
    rows = [dict(window_start=a, window_stop=b, samples=int(seg.stop - seg.start),
                 burner_on_samples=int(np.sum(bp[seg] > 0.0)),
                 pump_on_samples=int(np.sum(pm[seg] > 0.0)),
                 relight_inside=relight,
                 relight_after=bool(np.any(bp[seg.stop:] > 0.0)),
                 classified_as="mid_run_cutoff" if relight else "run_down")]
    pd.DataFrame(rows).to_csv(OUT / "tune1_late_window.csv", index=False)
    SUMMARY["tune1_late_window"] = rows


# ---------------------------------------------------------------------------
# 1.7 Cold-start transient length
# ---------------------------------------------------------------------------
def diag_cold_transient(con) -> None:
    rows = []
    for eid in ("TUNE-1", "VAL-5"):
        meta = v6.EPISODES[eid]
        df = v6.load_episode(con, meta["t_start"], meta["t_end"])
        wins = v6.detect_shutdown_windows(df)
        if not wins["cold"]:
            continue
        start = wins["cold"][0]
        for label, params, mode in (
            ("v5", dict(v6.V5_FROZEN), "v5"),
            ("v6.2", PARAMS, "v6"),
        ):
            res = v6.simulate_episode(df, params, mode=mode, reanchor_index=WARM)
            e = np.abs(res["sim"] - res["tinc_true"])
            k = start
            while k < len(df) and e[k] > 1.0:
                k += 1
            rows.append(dict(episode=eid, model=label, cold_start=int(start),
                             duration_s=float((k - start) * DT),
                             first_below_1c_index=int(k),
                             late_rmse_c=float(np.sqrt(np.mean(
                                 e[start + 30:start + 120] ** 2)))
                             if start + 120 <= len(df) else float("nan")))
    pd.DataFrame(rows).to_csv(OUT / "cold_start_transient.csv", index=False)
    SUMMARY["cold_start_transient"] = rows


# ---------------------------------------------------------------------------
# 1.8 Cutoff dip mechanism
# ---------------------------------------------------------------------------
def diag_cutoff_dip(con) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = []
    for eid in ("TUNE-1", "TUNE-3"):
        meta = v6.EPISODES[eid]
        df = v6.load_episode(con, meta["t_start"], meta["t_end"])
        bp = df["burner_pct"].values
        pm = df["pump_mod"].values
        tb = df["t_boiler"].values
        ti = df["t_inc"].values
        falls = np.where((bp[:-1] > 0.0) & (bp[1:] == 0.0))[0] + 1
        events = []
        for i in falls:
            if i < WARM or i + 60 >= len(df):
                continue
            if pm[i] <= 0.0:
                continue
            post = slice(i, i + 60)
            j = int(np.argmin(ti[post])) + i
            dip = float(ti[i] - ti[j])
            dpump = float(pm[i] - pm[j])
            dtb = float(tb[j] - tb[i])
            rows.append(dict(episode=eid, edge_index=int(i),
                             edge_time=str(df["t"].iloc[i]),
                             tinc_dip_c=dip, pump_mod_drop_pct=dpump,
                             tboiler_rise_c=dtb,
                             coincident=bool(dip > 0.5 and dpump > 5.0 and dtb > 0.0)))
            events.append(i)
        if events:
            fig, ax = plt.subplots(figsize=(12, 5))
            t = np.arange(len(df)) * DT / 60.0
            ax.plot(t, ti, color="black", lw=1.4, label="T_incoming")
            ax.plot(t, tb, color="crimson", lw=1.1, alpha=0.8, label="T_boiler")
            ax2 = ax.twinx()
            ax2.plot(t, pm, color="green", lw=1.0, ls="--",
                     label="pump_mod (%)")
            ax2.plot(t, bp, color="darkorange", lw=1.0, ls=":",
                     label="burner (%)")
            ax2.set_ylabel("Modulation (%)")
            for i in events:
                ax.axvline(i * DT / 60.0, color="purple", lw=0.8, alpha=0.5)
            ax.set_title(f"{eid}: burner-off events, pump_mod as a value")
            ax.set_ylabel("Temperature (C)")
            ax.set_xlabel("Elapsed minutes from warm-up start")
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
            ax.grid(True, ls=":", alpha=0.6)
            fig.tight_layout()
            fig.savefig(OUT / f"{eid.lower()}_cutoff_mechanism.png", dpi=150)
            plt.close(fig)
    pd.DataFrame(rows).to_csv(OUT / "cutoff_dip_mechanism.csv", index=False)
    SUMMARY["cutoff_dip_mechanism"] = rows


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)
    print("1.1 VAL-5 branch/window breakdown")
    diag_val5(con)
    print("1.2 stale-hold cause + raw reporting intervals")
    diag_stale(con)
    print("1.3 sensor quantisation")
    diag_quantisation(con)
    print("1.4 split overlap pre-fix")
    diag_split_overlap()
    print("1.5 FIT-DHW delay side effect")
    diag_fit_dhw(con)
    print("1.6 TUNE-1 late window classification")
    diag_tune1_rundown(con)
    print("1.7 cold-start transient length")
    diag_cold_transient(con)
    print("1.8 cutoff-dip mechanism")
    diag_cutoff_dip(con)
    write_summary()
    print(f"Wrote diagnostics to {OUT}")


if __name__ == "__main__":
    main()
