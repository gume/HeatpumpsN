"""The tool set agents work with.

Every tool is read-only towards the house: it reads the registry, the catalog,
the house description and the recorded data (read-only token), and writes
only into the run's own workspace. Generated code runs in the sandbox.

A tool is a name, a description, a JSON schema for its arguments and a
function; the same definitions serve the agent loop (OpenAI-style function
calling) and the MCP server. Results are JSON-able dicts; large data goes to
files in the workspace (``data/*.csv``) and only a summary comes back.
"""
from __future__ import annotations

import csv
import math
import os
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .data import AGGREGATES, DataSource
from .sandbox import Sandbox

EVENT = "heatpumpsn/controller/log/event"
SEVERITY = {"info": 0, "warn": 1, "warning": 1, "error": 2, "alarm": 2, "critical": 2}


def _severity(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return SEVERITY.get(str(value or "").lower(), 0)
TEXT_LIMIT = 15_000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]

    def schema(self) -> dict:
        """OpenAI-style function definition."""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _obj(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


def _iso(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def _num(x) -> float | None:
    return round(x, 4) if isinstance(x, (int, float)) and math.isfinite(x) else None


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:60] or "series"


class Toolbox:
    """Tools bound to one workspace. `service` provides signals() and house()."""

    def __init__(self, service, data: DataSource | None, sandbox: Sandbox | None, workspace: str,
                 context: str = ""):
        self.service, self.data, self.sandbox = service, data, sandbox
        self.workspace = os.path.abspath(workspace)
        self.context = context
        self.finished: dict | None = None
        self.tools: dict[str, Tool] = {t.name: t for t in self._define()}

    # --- dispatch ------------------------------------------------------------------
    def call(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}; available: {', '.join(self.tools)}"}
        try:
            return tool.fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except (ValueError, KeyError, FileNotFoundError, PermissionError) as exc:
            return {"error": str(exc)}

    def schemas(self, names: list[str] | None = None) -> list[dict]:
        return [t.schema() for n, t in self.tools.items() if names is None or n in names]

    # --- helpers -------------------------------------------------------------------
    def _rows(self) -> dict[str, dict]:
        return {r["id"]: r for r in self.service.signals()}

    def _known(self, ids: list[str]) -> list[str]:
        if not ids:
            raise ValueError("give at least one signal id")
        rows = self._rows()
        unknown = [i for i in ids if i not in rows]
        if unknown:
            raise ValueError(f"unknown signal ids {unknown}: use find_signals for the exact ids")
        return ids

    def _need_data(self) -> DataSource:
        if self.data is None:
            raise ValueError("no database access configured (OBSERVER_INFLUX_READ_TOKEN)")
        return self.data

    def _path(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.workspace, path))
        if not (full == self.workspace or full.startswith(self.workspace + os.sep)):
            raise PermissionError("paths are relative to the workspace and stay inside it")
        return full

    # --- the tools -------------------------------------------------------------------
    def house_context(self) -> dict:
        h = self.service.house()
        cur, draft = h.get("current") or {}, h.get("draft") or {}
        if cur.get("description"):
            text, status = cur["description"], f"accepted by the owner ({cur.get('accepted')})"
            checks = ((cur.get("model") or {}).get("checks")) or []
            knowledge = cur.get("knowledge") or ""
        elif draft:
            text, status = draft.get("description_md", ""), "DRAFT, not yet accepted by the owner"
            checks = ((draft.get("model") or {}).get("checks")) or []
            knowledge = draft.get("knowledge_md") or ""
        else:
            text, status, checks, knowledge = "", "no house understanding yet", [], ""
        return {"status": status, "configured_context": self.context.strip(),
                "owner_knowledge_md": knowledge[:6000],
                "understanding_md": text[:TEXT_LIMIT], "checks": checks[:40],
                "owner_review_not_yet_consolidated": (h.get("notes") or "")[:4000]}

    def find_signals(self, text: str = "", location: str = "", circuit: str = "", role: str = "",
                     quantity: str = "", source: str = "", stored_only: bool = True, limit: int = 40) -> dict:
        words = [w.lower() for w in text.split()]
        out = []
        for r in self._rows().values():
            info = r.get("info") or {}
            if stored_only and not r.get("store"):
                continue
            if source and r["source"] != source:
                continue
            if any(v and str(info.get(k, "")).lower() != v.lower() for k, v in
                   (("location", location), ("circuit", circuit), ("role", role))):
                continue
            if quantity and (r.get("quantity") or "").lower() != quantity.lower():
                continue
            hay = " ".join(str(x) for x in (r["id"], info.get("name"), info.get("description"), r.get("device"),
                                            (r.get("meta") or {}).get("name"))).lower()
            if any(w not in hay for w in words):
                continue
            out.append({"id": r["id"], "name": info.get("name") or (r.get("meta") or {}).get("name"),
                        "unit": r.get("unit"), "quantity": r.get("quantity"), "location": info.get("location"),
                        "circuit": info.get("circuit"), "role": info.get("role"),
                        "description": info.get("description"), "stored": r.get("store"),
                        "last": r.get("value") if r.get("value") is not None else r.get("text"),
                        "age_s": r.get("age_s")})
        out.sort(key=lambda s: s["id"])
        return {"count": len(out), "signals": out[:max(1, min(int(limit), 200))],
                "truncated": len(out) > limit}

    def get_series(self, signals: list[str], start: str = "-6h", stop: str = "now", every: str = "1m",
                   agg: str = "mean", file: str = "") -> dict:
        ids = self._known(list(signals))
        data = self._need_data().series(ids, start, stop, every, agg)
        name = _safe_name(file or "_".join(i.rsplit("/", 1)[-1] for i in ids)[:50]) + ".csv"
        path = os.path.join("data", name)
        os.makedirs(self._path("data"), exist_ok=True)
        with open(self._path(path), "w", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            if every == "raw":
                w.writerow(["time", "signal", "value"])
                rows = sorted((t, s.signal, v) for s in data.values() for t, v in zip(s.times, s.values))
                for t, sig, v in rows:
                    w.writerow([_iso(t), sig, v])
            else:
                w.writerow(["time", *ids])
                table: dict = {}
                for s in data.values():
                    for t, v in zip(s.times, s.values):
                        table.setdefault(t, {})[s.signal] = v
                for t in sorted(table):
                    w.writerow([_iso(t), *[table[t].get(i, "") for i in ids]])
        summary = {}
        for i in ids:
            s = data.get(i)
            if not s or not s.values:
                summary[i] = {"points": 0}
                continue
            nums = [v for v in s.values if isinstance(v, (int, float))]
            step = max(1, len(s.values) // 12)
            summary[i] = {"points": len(s.values), "first": _iso(s.times[0]), "last": _iso(s.times[-1]),
                          "sample": [[_iso(t), _num(v) if isinstance(v, (int, float)) else v]
                                     for t, v in list(zip(s.times, s.values))[::step][:12]]}
            if nums:
                summary[i].update(min=_num(min(nums)), max=_num(max(nums)), mean=_num(statistics.fmean(nums)),
                                  last_value=_num(nums[-1]))
            else:
                summary[i]["values"] = sorted({str(v) for v in s.values})[:10]
        return {"file": path, "format": "long: time,signal,value" if every == "raw" else "wide: time + one column per signal",
                "every": every, "agg": agg, "times": "UTC", "summary": summary}

    def signal_features(self, signal: str, start: str = "-24h", stop: str = "now") -> dict:
        self._known([signal])
        s = self._need_data().series([signal], start, stop, "raw").get(signal)
        if not s or not s.values:
            return {"signal": signal, "points": 0, "note": "no samples in this range"}
        ts = [t.timestamp() for t in s.times]
        vals = [v for v in s.values if isinstance(v, (int, float))]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        out = {"signal": signal, "points": len(ts), "first": _iso(s.times[0]), "last": _iso(s.times[-1])}
        if gaps:
            worst = max(range(len(gaps)), key=gaps.__getitem__)
            out.update(median_interval_s=_num(statistics.median(gaps)), max_gap_s=_num(gaps[worst]),
                       max_gap_at=_iso(s.times[worst]))
        if len(vals) != len(ts):
            out["values"] = sorted({str(v) for v in s.values})[:20]
            return out
        out.update(min=_num(min(vals)), max=_num(max(vals)), mean=_num(statistics.fmean(vals)),
                   stdev=_num(statistics.pstdev(vals)), distinct=len(set(vals)))
        # Longest run of an unchanged value (a stuck sensor reports the same value for long).
        best, run_start = (0.0, 0), 0
        for i in range(1, len(vals) + 1):
            if i == len(vals) or vals[i] != vals[run_start]:
                span = ts[i - 1] - ts[run_start]
                if span > best[0]:
                    best = (span, run_start)
                run_start = i
        out["longest_constant_s"] = _num(best[0])
        out["longest_constant_value"] = _num(vals[best[1]])
        out["longest_constant_from"] = _iso(s.times[best[1]])
        if len(ts) > 2 and ts[-1] > ts[0]:
            mt, mv = statistics.fmean(ts), statistics.fmean(vals)
            den = sum((t - mt) ** 2 for t in ts)
            out["trend_per_h"] = _num(sum((t - mt) * (v - mv) for t, v in zip(ts, vals)) / den * 3600) if den else 0
        if set(vals) <= {0.0, 1.0}:
            ons, on_since = [], None
            for t, v in zip(ts, vals):
                if v == 1.0 and on_since is None:
                    on_since = t
                elif v == 0.0 and on_since is not None:
                    ons.append(t - on_since)
                    on_since = None
            total = ts[-1] - ts[0]
            out.update(switch_ons=len(ons) + (1 if on_since is not None else 0),
                       on_time_h=_num((sum(ons) + ((ts[-1] - on_since) if on_since else 0)) / 3600),
                       duty=_num((sum(ons) + ((ts[-1] - on_since) if on_since else 0)) / total) if total else None,
                       shortest_on_s=_num(min(ons)) if ons else None,
                       median_on_s=_num(statistics.median(ons)) if ons else None)
        return out

    def get_events(self, start: str = "-24h", stop: str = "now", min_severity: int = 0, text: str = "",
                   limit: int = 100) -> dict:
        fields = [f"{EVENT}#{f}" for f in ("code", "severity", "detail")]
        known = self._rows()
        fields = [f for f in fields if f in known]
        if not fields:
            return {"events": [], "note": "the controller's event log is not recorded"}
        data = self._need_data().series(fields, start, stop, "raw")
        by_time: dict = {}
        for s in data.values():
            for t, v in zip(s.times, s.values):
                by_time.setdefault(t.replace(microsecond=0), {})[s.signal.split("#")[1]] = v
        events = []
        for t in sorted(by_time):
            e = by_time[t]
            sev = _severity(e.get("severity"))
            if sev < min_severity or (text and text.lower() not in str(e.get("detail", "")).lower()):
                continue
            events.append({"time": _iso(t), "code": int(e["code"]) if e.get("code") is not None else None,
                           "severity": e.get("severity"), "detail": e.get("detail")})
        return {"count": len(events), "events": events[-max(1, min(int(limit), 500)):], "times": "UTC",
                "min_severity": "0 info, 1 warn, 2 error"}

    def run_python(self, code: str, purpose: str = "") -> dict:
        if self.sandbox is None:
            raise ValueError("no sandbox configured")
        st = self.sandbox.status()
        if not st["ready"]:
            raise ValueError("sandbox not ready: " + "; ".join(st["problems"]))
        result = self.sandbox.run(code, self.workspace, _safe_name(purpose or "snippet")[:30])
        return result.as_dict()

    def list_files(self) -> dict:
        files = []
        if not os.path.isdir(self.workspace):
            return {"files": files}
        for base, dirs, names in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                p = os.path.join(base, n)
                files.append({"path": os.path.relpath(p, self.workspace), "bytes": os.path.getsize(p)})
        return {"files": sorted(files, key=lambda f: f["path"])}

    def read_file(self, path: str, max_chars: int = 8000) -> dict:
        full = self._path(path)
        with open(full, encoding="utf-8", errors="replace") as fp:
            text = fp.read(max(1, min(int(max_chars), 50_000)) + 1)
        return {"path": path, "text": text[:max_chars], "truncated": len(text) > max_chars}

    def finish(self, report_md: str, findings: list | None = None, confidence: float = 0.5,
               proposals: list | None = None) -> dict:
        self.finished = {"report_md": report_md, "findings": findings or [],
                         "confidence": max(0.0, min(1.0, float(confidence))), "proposals": proposals or []}
        return {"ok": True}

    # --- definitions ---------------------------------------------------------------------
    def _define(self) -> list[Tool]:
        time_doc = "'now', a relative duration like -6h / -2d, or an ISO time (UTC unless an offset is given)"
        return [
            Tool("house_context", "The house understanding (circuits, rooms, sensors, expected behaviour, checks), "
                 "the owner knowledge (facts not in the data: building, comfort targets, history; authoritative) "
                 "and the owner's latest review. Read it first.", _obj({}), self.house_context),
            Tool("find_signals", "Search the signal registry: ids, names, units, location (floor), circuit, role. "
                 "Use the returned exact ids with the data tools.",
                 _obj({"text": {"type": "string", "description": "words that must all appear in id, name, "
                                "description or device"},
                       "location": {"type": "string"}, "circuit": {"type": "string"},
                       "role": {"type": "string", "description": "supply, return, room, outdoor, boiler, dhw, pump, "
                                "valve, burner, controller, ..."},
                       "quantity": {"type": "string", "description": "temperature, humidity, power, state, ..."},
                       "source": {"type": "string", "description": "heatpumpsn, ems-esp, ha_state, zigbee2mqtt, "
                                  "mixermeter"},
                       "stored_only": {"type": "boolean", "description": "only signals with recorded data "
                                       "(default true)"},
                       "limit": {"type": "integer"}}), self.find_signals),
            Tool("get_series", "Recorded time series of one or more signals. Writes a CSV into the workspace "
                 "(data/<file>.csv, for run_python) and returns per-signal statistics and a short sample.",
                 _obj({"signals": {"type": "array", "items": {"type": "string"}},
                       "start": {"type": "string", "description": time_doc},
                       "stop": {"type": "string", "description": time_doc},
                       "every": {"type": "string", "description": "'raw' for every sample, or a window like 30s, "
                                 "1m, 15m, 1h (default 1m)"},
                       "agg": {"type": "string", "enum": list(AGGREGATES)},
                       "file": {"type": "string", "description": "file name without extension"}},
                      ("signals",)), self.get_series),
            Tool("signal_features", "Computed features of one signal over a range: sample interval and largest "
                 "gap, min/max/mean/stdev, longest constant run (stuck sensor), trend per hour, and for on/off "
                 "signals the switch-ons, on time, duty cycle and shortest on-period (short cycling).",
                 _obj({"signal": {"type": "string"}, "start": {"type": "string", "description": time_doc},
                       "stop": {"type": "string", "description": time_doc}}, ("signal",)), self.signal_features),
            Tool("get_events", "The heating controller's event log (code, severity, text) over a range.",
                 _obj({"start": {"type": "string", "description": time_doc},
                       "stop": {"type": "string", "description": time_doc},
                       "min_severity": {"type": "integer", "description": "0 all, 1 warnings and errors, 2 errors only"},
                       "text": {"type": "string", "description": "only events whose text contains this"},
                       "limit": {"type": "integer"}}), self.get_events),
            Tool("run_python", "Run Python 3 code in the sandbox (no network, no database; numpy, pandas, scipy, "
                 "matplotlib). The workspace is the current directory: read data/*.csv written by get_series, "
                 "write results and plots (PNG) there. Print what you need to see; output is truncated.",
                 _obj({"code": {"type": "string"},
                       "purpose": {"type": "string", "description": "a few words, used as the script name"}},
                      ("code",)), self.run_python),
            Tool("list_files", "Files in the run's workspace.", _obj({}), self.list_files),
            Tool("read_file", "Read a text file from the workspace.",
                 _obj({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ("path",)), self.read_file),
            Tool("finish", "End the run with the report for the owner.",
                 _obj({"report_md": {"type": "string", "description": "Markdown report: question, what was "
                                     "checked, findings with evidence (signal ids, times, values), what to do"},
                       "findings": {"type": "array", "items": _obj({
                           "title": {"type": "string"},
                           "severity": {"type": "string", "enum": ["info", "warning", "alert"]},
                           "evidence": {"type": "string"}}, ("title", "severity"))},
                       "confidence": {"type": "number", "description": "0..1"},
                       "proposals": {"type": "array", "items": {"type": "string"},
                                     "description": "changes the owner could approve (never applied by the agent)"}},
                      ("report_md",)), self.finish),
        ]
