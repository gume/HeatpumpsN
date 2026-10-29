"""Run logging: JSONL files under <log-dir>/<session-id>/, rotated daily.

DuckDB reads them directly, e.g.
    SELECT * FROM read_json_auto('logs/*/hil_timeseries_*.jsonl');
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

TIMESERIES_FIELDS = (
    "timestamp", "t_boiler_primary", "t_inc_water", "t_inc_sensor", "t_fwd_mix", "t_fwd_sensor",
    "t_fwd_setpoint", "t_ret", "t_slab", "t_room", "t_out", "p1", "p2", "p3", "dhw_active",
    "burner_pct", "mixer_state_pct", "mixer_integrator", "system_error_code",
    "mpc_mode", "mpc_bias", "mpc_scenario", "controller_ok")
EVENT_FIELDS = (
    "timestamp_start", "timestamp_end", "duration_s", "direction", "pos_before_pct", "pos_after_pct",
    "delta_pos_pct", "t_fwd", "setpoint", "p1_active", "p2_active")


def git_head(cwd: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


class LogStore:
    def __init__(self, log_dir: Path, meta: dict):
        self.session_id = str(uuid.uuid4())
        self.dir = Path(log_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.session_id[:8]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = dict(session_id=self.session_id, started=time.time(),
                         git_commit=git_head(Path(__file__).parent), **meta)
        (self.dir / "session.json").write_text(json.dumps(self.meta, indent=2, default=str))
        self._files: dict[str, tuple[str, object]] = {}

    def _write(self, name: str, row: dict):
        day = time.strftime("%Y%m%d")
        cur = self._files.get(name)
        if cur is None or cur[0] != day:
            if cur is not None:
                cur[1].close()
            fh = open(self.dir / f"{name}_{day}.jsonl", "a", buffering=1)
            self._files[name] = (day, fh)
        row = dict(row, session_id=self.session_id)
        self._files[name][1].write(json.dumps(row, default=float) + "\n")

    def timeseries(self, row: dict):
        self._write("hil_timeseries", {k: row.get(k) for k in TIMESERIES_FIELDS})

    def event(self, row: dict):
        self._write("mixer_actuation_events", {k: row.get(k) for k in EVENT_FIELDS})

    def close(self):
        for _, fh in self._files.values():
            fh.flush()
            fh.close()
        self._files.clear()
        self.meta["ended"] = time.time()
        (self.dir / "session.json").write_text(json.dumps(self.meta, indent=2, default=str))
