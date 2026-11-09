"""The core tools the model calls (function calling, or MCP).

Read-only towards the house: they read the registry, the catalog, the house
understanding and the recorded data (read-only token), write only into the
run's workspace, and into the house part of the library (new tools and
skills, always unaudited). Code runs in the sandbox, where it reads the data
itself through the run's data socket (``obs.data``) and uses the library's
analysis tools (``obs.tools``).

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
                 context: str = "", library=None, author: str = ""):
        self.service, self.data, self.sandbox = service, data, sandbox
        self.workspace = os.path.abspath(workspace)
        self.context = context
        self.library, self.author = library, author
        self.broker = None                 # set by the runner: the run's data socket for sandboxed code
        self.skills_read: dict[str, bool] = {}
        self.created: list[str] = []
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
    def mpc_params(self) -> dict:
        """The controller's parameter sets as it last published them ({scenario: params}); {} when unknown."""
        try:
            return dict(self.service.parameters.current_sets())
        except Exception:  # noqa: BLE001 - a service without the parameter path (tests, MCP)
            return {}

    def house_context(self) -> dict:
        h = self.service.house()
        cur, draft = h.get("current") or {}, h.get("draft") or {}
        owner = h.get("owner") or {}
        if cur.get("description"):
            text = cur["description"]
            status = f"accepted by the owner ({cur['accepted']})" if cur.get("accepted") else "accepted by the owner"
            checks = ((cur.get("model") or {}).get("checks")) or []
        elif draft:
            text, status = draft.get("description_md", ""), "DRAFT, not yet accepted by the owner"
            checks = ((draft.get("model") or {}).get("checks")) or []
        else:
            text, status, checks = "", "no house description yet", []
        return {"status": status, "configured_context": self.context.strip(),
                "owner_description_md": (owner.get("description") or cur.get("knowledge") or "")[:6000],
                "house_description_md": text[:TEXT_LIMIT], "checks": checks[:40],
                "previous_owner_description_available": bool(owner.get("can_undo"))}

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

    def data_coverage(self, signals: list[str]) -> dict:
        ids = self._known(list(signals))
        return {"coverage": self._need_data().coverage(ids), "times": "UTC"}

    def run_python(self, code: str, purpose: str = "") -> dict:
        if self.sandbox is None:
            raise ValueError("no sandbox configured")
        st = self.sandbox.status()
        if not st["ready"]:
            raise ValueError("sandbox not ready: " + "; ".join(st["problems"]))
        os.makedirs(self.workspace, exist_ok=True)
        result = self.sandbox.run(code, self.workspace, _safe_name(purpose or "snippet")[:30],
                                  socket_dir=self.broker.dir if self.broker else None)
        return result.as_dict()

    # --- the library: analysis tools and skills -------------------------------------------
    def _need_library(self):
        if self.library is None:
            raise ValueError("no library configured")
        return self.library

    def list_library(self, kind: str = "tools") -> dict:
        items = self._need_library().items(kind)
        return {"kind": kind, "items": [i.summary() for i in items.values()],
                "note": "audited = checked and approved by the owner; prefer audited items and say in the "
                        "report when a result depends on an unaudited one"}

    def read_library_item(self, kind: str, name: str) -> dict:
        item = self._need_library().get(kind, name)
        if kind == "skills":
            self.skills_read[name] = item.audited
        return item.detail()

    def create_tool(self, name: str, purpose: str, inputs: list, outputs: dict, code: str, tests: str,
                    needs: str = "") -> dict:
        lib = self._need_library()
        existing = lib.items("tools").get(name)
        if existing is not None and existing.audited and existing.origin == "house":
            raise PermissionError(f"tool {name!r} is audited; create a new name instead of changing it")
        item = lib.save_tool(name, purpose, inputs, outputs, code, tests, needs, author=self.author)
        self.created.append(f"tools/{name}")
        result = lib.run_tests(name, self.sandbox) if self.sandbox else {"passed": None, "output": "no sandbox"}
        return {"name": name, "origin": item.origin, "version": item.meta.get("version"), "audited": False,
                "tests_passed": result["passed"], "test_output": result["output"][-3000:],
                "use": f"obs.tools.load({name!r}).run(...) in run_python"}

    def save_skill(self, name: str, title: str, description: str, recipe: str, tools: list | None = None) -> dict:
        lib = self._need_library()
        existing = lib.items("skills").get(name)
        if existing is not None and existing.audited and existing.origin == "house":
            raise PermissionError(f"skill {name!r} is audited; save under a new name")
        item = lib.save_skill(name, title, description, recipe, tools, author=self.author)
        self.created.append(f"skills/{name}")
        return {"name": name, "origin": item.origin, "audited": False}

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
            Tool("house_context", "The house description (circuits, rooms, sensors, expected behaviour, checks), "
                 "the owner's description (facts not in the data: building, comfort targets, history; authoritative) "
                 "and whether a previous owner description exists. Read it first.", _obj({}), self.house_context),
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
            Tool("data_coverage", "How much history each signal has: first and last sample time and the number "
                 "of samples. Use it to choose time windows.",
                 _obj({"signals": {"type": "array", "items": {"type": "string"}}}, ("signals",)), self.data_coverage),
            Tool("run_python", "Run Python 3 code in the sandbox (numpy, pandas, scipy, matplotlib; no network). "
                 "The code reads the recorded data itself, read-only: `from obs import data` -> "
                 "data.series(ids, start, stop, every, agg) returns a pandas DataFrame (UTC index, one column per "
                 "signal), data.find(...), data.coverage(ids), data.events(...), data.house(). Library tools: "
                 "`from obs import tools` -> tools.load(name).run(...), tools.list(). The controller's mixer "
                 "control in simulation (its own MPC core): `from obs import sim` -> sim.closed_loop(...), "
                 "sim.replay(start, stop, sets=...), sim.compare(...), sim.current_sets(); help(sim). "
                 "The workspace is the current "
                 "directory (data/*.csv from get_series, your results, PNG plots). Print what you need to see; "
                 "output is truncated.",
                 _obj({"code": {"type": "string"},
                       "purpose": {"type": "string", "description": "a few words, used as the script name"}},
                      ("code",)), self.run_python),
            Tool("list_library", "The library: analysis tools (code usable in run_python, with purpose, inputs, "
                 "outputs, test status) or skills (recipes), each with its audit flag.",
                 _obj({"kind": {"type": "string", "enum": ["tools", "skills"]}}), self.list_library),
            Tool("read_library_item", "Full content of a library tool (manifest, code, tests, last test result) or "
                 "skill (recipe).",
                 _obj({"kind": {"type": "string", "enum": ["tools", "skills"]}, "name": {"type": "string"}},
                      ("kind", "name")), self.read_library_item),
            Tool("create_tool", "Add a reusable analysis tool to the house library (unaudited until the owner "
                 "checks it); its tests run at once in the sandbox. Make it universal: generic inputs (series, data "
                 "frames, numbers), no house-specific signal ids, documented purpose, inputs and output, and tests on "
                 "synthetic data (`from tool import run`). Saving an existing unaudited house tool replaces it.",
                 _obj({"name": {"type": "string", "description": "snake_case"},
                       "purpose": {"type": "string", "description": "what it is for and how it works"},
                       "needs": {"type": "string", "description": "what kind of data it needs, in generic terms"},
                       "inputs": {"type": "array", "items": _obj({
                           "name": {"type": "string"},
                           "type": {"type": "string", "enum": ["series", "dataframe", "number", "integer", "string",
                                                               "boolean", "list", "dict", "timestamp", "any"]},
                           "description": {"type": "string"}, "required": {"type": "boolean"}}, ("name", "type"))},
                       "outputs": _obj({"type": {"type": "string"}, "description": {"type": "string"}},
                                       ("description",)),
                       "code": {"type": "string", "description": "Python module defining run(...)"},
                       "tests": {"type": "string", "description": "pytest module: from tool import run; def test_..."}},
                      ("name", "purpose", "inputs", "outputs", "code", "tests")), self.create_tool),
            Tool("save_skill", "Save a recipe to the house library (unaudited until the owner checks it): a method "
                 "that worked, domain knowledge for a recurring question. Write it in generic terms (circuits, roles), "
                 "naming the library tools it uses.",
                 _obj({"name": {"type": "string", "description": "snake_case"}, "title": {"type": "string"},
                       "description": {"type": "string"}, "recipe": {"type": "string", "description": "Markdown steps"},
                       "tools": {"type": "array", "items": {"type": "string"}}},
                      ("name", "title", "description", "recipe")), self.save_skill),
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
