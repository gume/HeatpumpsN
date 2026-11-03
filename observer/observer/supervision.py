"""Supervision: automatic checks that open and close alerts (plan phase C).

Checks are defined in ``<OBSERVER_HOME>/checks.yaml`` (created from
``defaults/checks.yaml``; editable on the portal's Alerts tab). Types:

* ``event``  — evaluated on every incoming message of one signal, no LLM:
  ``when`` opens an alert (``equals``, ``not_equals``, ``in``, ``not_in``,
  ``above``, ``below``, ``changed``); it closes on ``clear_when`` or after
  ``clear_after`` without a new match. ``subject`` (a sibling field of the
  same message, e.g. ``#code``) gives one alert per subject; ``detail`` adds
  another field to the text.
* ``stale``  — on a schedule: no new sample for ``max_age`` (``mode: source``:
  the newest of all selected signals, e.g. a whole device; ``mode: each``:
  per signal).
* ``range``  — on a schedule: the latest value outside ``min`` / ``max``.
* ``stuck``  — on a schedule, from the database: no change at all for
  ``hours`` while samples keep arriving.
* ``agent``  — on a schedule: an agent run with a skill (uses the LLM); its
  findings of severity warning/alert become alerts. Off by default.
* ``identify`` — on a schedule: re-identifies the controller's mixer model from
  live data (parameters.py, no LLM); proposals and a drift alert follow.

Schedules: ``every: 5m`` or ``cron: "17 6 * * *"`` (local time). Signals are
chosen with ``signal: <id>`` or ``select: {source, role, quantity, location,
circuit, ids, stored}``. Severities: info, warning, alarm.

Alerts open once, count repeats, close by themselves when the condition is
gone, and can be acknowledged. Open alerts are published over MQTT with Home
Assistant discovery (a problem binary sensor and an alert count), so Home
Assistant can notify.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime

import yaml

log = logging.getLogger(__name__)
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults", "checks.yaml")
SEVERITIES = ("info", "warning", "alarm")
TYPES = ("event", "stale", "range", "stuck", "agent", "identify")


def duration_s(text) -> float:
    """'90s', '5m', '2h', '1d' or a number of seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text or ""))
    if not m:
        raise ValueError(f"duration {text!r}: e.g. 90s, 5m, 2h, 1d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def human_age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


class Cron:
    """Five fields: minute hour day-of-month month day-of-week (0 or 7 = Sunday); *, lists, ranges, steps."""

    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expr: str):
        parts = str(expr).split()
        if len(parts) != 5:
            raise ValueError(f"cron {expr!r}: five fields: minute hour day month weekday")
        self.sets = [self._field(p, lo, hi) for p, (lo, hi) in zip(parts, self.RANGES)]
        if 7 in self.sets[4]:
            self.sets[4].add(0)

    @staticmethod
    def _field(text: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for part in text.split(","):
            step = 1
            if "/" in part:
                part, s = part.split("/", 1)
                step = int(s)
            if part == "*":
                a, b = lo, hi
            elif "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
            else:
                a = b = int(part)
            if not (lo <= a <= hi and lo <= b <= hi) or step < 1:
                raise ValueError(f"cron field {text!r} outside {lo}-{hi}")
            out.update(range(a, b + 1, step))
        return out

    def matches(self, t: datetime) -> bool:
        m, h, dom, mon, dow = self.sets
        return (t.minute in m and t.hour in h and t.day in dom and t.month in mon
                and (t.isoweekday() % 7) in dow)


def _cmp(value, text, cond: dict) -> bool:
    """One condition against a sample (numeric value or text)."""
    v = text if value is None else value
    sv = "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
    for op, arg in cond.items():
        if op == "equals" and not (sv == str(arg) or (isinstance(v, float) and _num(arg) == v)):
            return False
        if op == "not_equals" and (sv == str(arg) or (isinstance(v, float) and _num(arg) == v)):
            return False
        if op == "in" and sv not in [str(a) for a in arg] and not (isinstance(v, float) and v in [_num(a) for a in arg]):
            return False
        if op == "not_in" and (sv in [str(a) for a in arg] or (isinstance(v, float) and v in [_num(a) for a in arg])):
            return False
        if op == "above" and not (isinstance(v, float) and v > float(arg)):
            return False
        if op == "below" and not (isinstance(v, float) and v < float(arg)):
            return False
    return True


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class AlertStore:
    """Open and recently closed alerts (data/alerts.json) and a history log (data/alerts.log.jsonl)."""

    def __init__(self, path: str):
        self.path, self.log_path = path, os.path.splitext(path)[0] + ".log.jsonl"
        self.alerts: dict[str, dict] = {}
        self._lock = threading.RLock()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                self.alerts = json.load(fp)

    def _save(self) -> None:
        with open(self.path + ".tmp", "w", encoding="utf-8") as fp:
            json.dump(self.alerts, fp, ensure_ascii=False, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def _history(self, event: str, a: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "key": a["key"],
                                 "severity": a["severity"], "title": a["title"]}, ensure_ascii=False) + "\n")

    def raise_(self, key: str, check: str, severity: str, title: str, detail: str = "", signal: str = "") -> bool:
        """Open the alert or count a repeat; True when it is new (or reopened)."""
        now = time.time()
        with self._lock:
            a = self.alerts.get(key)
            if a and a["state"] in ("open", "acknowledged"):
                a.update(last=now, count=a["count"] + 1, title=title, detail=detail or a.get("detail", ""))
                if SEVERITIES.index(severity) > SEVERITIES.index(a["severity"]):
                    a["severity"] = severity
                self._save()
                return False
            a = {"key": key, "check": check, "severity": severity, "title": title, "detail": detail, "signal": signal,
                 "first": now, "last": now, "count": 1, "state": "open"}
            self.alerts[key] = a
            self._history("open", a)
            self._save()
            return True

    def resolve(self, key: str) -> bool:
        with self._lock:
            a = self.alerts.get(key)
            if not a or a["state"] == "resolved":
                return False
            a.update(state="resolved", resolved=time.time())
            self._history("resolved", a)
            self._save()
            return True

    def acknowledge(self, key: str) -> bool:
        with self._lock:
            a = self.alerts.get(key)
            if not a or a["state"] != "open":
                return False
            a.update(state="acknowledged", acknowledged=time.time())
            self._history("acknowledged", a)
            self._save()
            return True

    def open_keys(self, check: str) -> list[str]:
        with self._lock:
            return [k for k, a in self.alerts.items() if a["check"] == check and a["state"] != "resolved"]

    def view(self, keep_resolved_s: float = 7 * 86400) -> list[dict]:
        now = time.time()
        with self._lock:
            for k in [k for k, a in self.alerts.items() if a["state"] == "resolved"
                      and now - a.get("resolved", now) > keep_resolved_s]:
                del self.alerts[k]
            order = {"open": 0, "acknowledged": 1, "resolved": 2}
            return sorted((dict(a) for a in self.alerts.values()),
                          key=lambda a: (order[a["state"]], -SEVERITIES.index(a["severity"]), -a["last"]))


class Supervisor:
    def __init__(self, service, cfg: dict):
        self.service, self.cfg = service, cfg
        home = os.path.dirname(cfg["config_path"]) if cfg.get("config_path") else os.path.dirname(cfg["paths"]["catalog"])
        self.checks_path = cfg["paths"].get("checks") or os.path.join(home, "checks.yaml")
        if not os.path.exists(self.checks_path):
            shutil.copyfile(DEFAULTS, self.checks_path)
        self.store = AlertStore(os.path.join(os.path.dirname(cfg["paths"]["catalog"]), "alerts.json"))
        a = cfg.get("alerts") or {}
        self.topic = (a.get("mqtt_topic") or "observer").strip("/")
        self.discovery = a.get("discovery_prefix", "homeassistant")
        self.publish_enabled = bool(a.get("publish", True))
        self.checks: list[dict] = []
        self.state: dict[str, dict] = {}         # per check: last run, next, result, error
        self.error: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._agent_runs: dict[str, str] = {}    # check id -> run id waiting for its findings
        self.load()

    # --- configuration -------------------------------------------------------------
    @staticmethod
    def validate(checks: list) -> list[dict]:
        if not isinstance(checks, list):
            raise ValueError("checks: a list")
        seen = set()
        for c in checks:
            if not isinstance(c, dict) or not c.get("id") or c.get("type") not in TYPES:
                raise ValueError(f"every check needs an id and a type ({', '.join(TYPES)}): {c!r}"[:300])
            if c["id"] in seen:
                raise ValueError(f"duplicate check id {c['id']!r}")
            seen.add(c["id"])
            if c.get("severity", "warning") not in SEVERITIES:
                raise ValueError(f"{c['id']}: severity is one of {SEVERITIES}")
            if c["type"] == "event":
                if not c.get("signal") or not isinstance(c.get("when"), dict):
                    raise ValueError(f"{c['id']}: an event check needs signal and when")
            elif c["type"] == "agent":
                if not c.get("cron") or not c.get("skill"):
                    raise ValueError(f"{c['id']}: an agent check needs cron and skill")
            elif c["type"] == "identify":
                if not (c.get("cron") or c.get("every")):
                    raise ValueError(f"{c['id']}: an identify check needs cron or every")
            elif not (c.get("every") or c.get("cron")):
                raise ValueError(f"{c['id']}: needs every or cron")
            if c.get("every"):
                duration_s(c["every"])
            if c.get("cron"):
                Cron(c["cron"])
            for k in ("max_age", "clear_after"):
                if c.get(k):
                    duration_s(c[k])
        return checks

    def load(self) -> None:
        with open(self.checks_path, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
        try:
            checks = self.validate(data.get("checks") or [])
            self.error = None
        except ValueError as exc:
            self.error = f"checks.yaml: {exc}"
            log.error(self.error)
            return
        with self._lock:
            self.checks = checks
            known = {c["id"] for c in checks}
            self.state = {k: v for k, v in self.state.items() if k in known}

    def text(self) -> str:
        return open(self.checks_path, encoding="utf-8").read()

    def save_text(self, text: str) -> None:
        data = yaml.safe_load(text) or {}
        self.validate(data.get("checks") or [])
        with open(self.checks_path + ".tmp", "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(self.checks_path + ".tmp", self.checks_path)
        self.load()

    # --- selection ---------------------------------------------------------------------
    def _rows(self) -> list[dict]:
        return self.service.signals()

    def select(self, c: dict) -> list[dict]:
        if c.get("signal"):
            return [r for r in self._rows() if r["id"] == c["signal"]]
        sel = c.get("select") or {}
        out = []
        for r in self._rows():
            info = r.get("info") or {}
            if sel.get("ids") and r["id"] not in sel["ids"]:
                continue
            if sel.get("source") and r["source"] != sel["source"]:
                continue
            if sel.get("quantity") and (r.get("quantity") or "") != sel["quantity"]:
                continue
            if any(sel.get(k) and (info.get(k) or "") != sel[k] for k in ("role", "location", "circuit")):
                continue
            if sel.get("stored", True) and not r.get("store"):
                continue
            out.append(r)
        return out

    @staticmethod
    def _name(r: dict) -> str:
        return (r.get("info") or {}).get("name") or (r.get("meta") or {}).get("name") or r["id"]

    # --- alerts --------------------------------------------------------------------------
    def _raise(self, c: dict, subject: str, title: str, detail: str = "", signal: str = "",
               severity: str | None = None) -> None:
        if self.store.raise_(f"{c['id']}:{subject}", c["id"], severity or c.get("severity", "warning"), title,
                             detail, signal):
            log.warning("alert: %s", title)
            self.publish()

    def _resolve(self, c: dict, subject: str) -> None:
        if self.store.resolve(f"{c['id']}:{subject}"):
            log.info("alert resolved: %s:%s", c["id"], subject)
            self.publish()

    @staticmethod
    def _fmt(template: str, **kw) -> str:
        try:
            return str(template).format(**kw)
        except (KeyError, IndexError, ValueError):
            return str(template)

    # --- event checks (on every message) ----------------------------------------------------
    def on_message(self, topic: str, samples: list, retained: bool, time_ns: int) -> None:
        if retained or not samples:
            return
        by_signal = {s.signal: s for s in samples}
        for c in self.checks:
            if c["type"] != "event" or c.get("enabled") is False or c["signal"] not in by_signal:
                continue
            s = by_signal[c["signal"]]
            stem = c["signal"].split("#", 1)[0]

            def field(ref: str):
                """Another field of the same message: "#code" next to "...#severity", or a full signal id."""
                x = by_signal.get(stem + ref if ref.startswith("#") else ref)
                if x is None:
                    return None
                return x.text if x.value is None else (f"{x.value:g}" if isinstance(x.value, float) else x.value)

            subject = str(field(c["subject"]) or "-") if c.get("subject") else "-"
            value = s.text if s.value is None else s.value
            st = self.state.setdefault(c["id"], {})
            if c["when"].get("changed"):
                prev = st.get("previous")
                st["previous"] = value
                hit = prev is not None and prev != value
            else:
                hit = _cmp(s.value, s.text, c["when"])
            st.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), last_value=value)
            vtxt = f"{value:g}" if isinstance(value, float) else str(value)
            if hit:
                detail = str(field(c["detail"]) or "") if c.get("detail") else ""
                self._raise(c, subject, self._fmt(c.get("title", c["id"] + ": {value}"), value=vtxt, detail=detail,
                                                  subject=subject, signal=c["signal"]), detail, c["signal"])
                st["matched"] = time.time()
            elif c.get("clear_when") and _cmp(s.value, s.text, c["clear_when"]):
                for key in self.store.open_keys(c["id"]):
                    if self.store.resolve(key):
                        self.publish()

    # --- periodic checks ----------------------------------------------------------------
    def due(self, c: dict, now: float) -> bool:
        st = self.state.setdefault(c["id"], {})
        if c.get("cron"):
            minute = int(now // 60)
            if st.get("cron_minute") == minute:
                return False
            if Cron(c["cron"]).matches(datetime.fromtimestamp(now)):
                st["cron_minute"] = minute
                return True
            return False
        return now - st.get("t", 0) >= duration_s(c.get("every", "5m"))

    def run_check(self, c: dict) -> dict:
        st = self.state.setdefault(c["id"], {})
        st["t"] = time.time()
        try:
            result = getattr(self, f"_check_{c['type']}")(c)
            st.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), result=result, error=None)
        except Exception as exc:
            st.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), error=f"{type(exc).__name__}: {exc}")
            log.warning("check %s failed: %s", c["id"], exc)
        return st

    def _check_stale(self, c: dict) -> str:
        rows, limit = self.select(c), duration_s(c.get("max_age", "10m"))
        if not rows:
            return "no signals selected"
        if c.get("mode", "source") == "source":
            age = min(r["age_s"] for r in rows)
            if age > limit:
                self._raise(c, "-", self._fmt(c.get("title", "{check}: no data for {age}"), age=human_age(age),
                                              check=c["id"], count=len(rows)),
                            f"newest of {len(rows)} signals is {human_age(age)} old")
                return f"silent for {human_age(age)}"
            self._resolve(c, "-")
            return f"ok (newest {human_age(age)} old)"
        bad = 0
        for r in rows:
            if r["age_s"] > limit:
                bad += 1
                self._raise(c, r["id"], self._fmt(c.get("title", "{name}: no data for {age}"), name=self._name(r),
                                                  age=human_age(r["age_s"])), signal=r["id"])
            else:
                self._resolve(c, r["id"])
        return f"{bad} of {len(rows)} silent"

    def _check_range(self, c: dict) -> str:
        rows, lo, hi = self.select(c), c.get("min"), c.get("max")
        fresh = duration_s(c.get("fresh", "30m"))
        bad = 0
        for r in rows:
            v = r.get("value")
            if not isinstance(v, (int, float)) or r["age_s"] > fresh:
                continue
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                bad += 1
                self._raise(c, r["id"], self._fmt(c.get("title", "{name}: {value} outside {min}..{max}"),
                                                  name=self._name(r), value=f"{v:g}", min=lo, max=hi,
                                                  unit=r.get("unit") or ""), signal=r["id"])
            else:
                self._resolve(c, r["id"])
        return f"{bad} of {len(rows)} outside"

    def _check_stuck(self, c: dict) -> str:
        data = self.service.agents.data
        if data is None:
            raise RuntimeError("no read-only database token (OBSERVER_INFLUX_READ_TOKEN)")
        rows = {r["id"]: r for r in self.select(c)}
        if not rows:
            return "no signals selected"
        hours = float(c.get("hours", 6))
        series = data.series(list(rows), f"-{int(hours * 60)}m", "now", "raw", limit=200_000)
        bad = 0
        for sid, r in rows.items():
            s = series.get(sid)
            vals = [v for v in (s.values if s else []) if isinstance(v, (int, float))]
            if len(vals) >= int(c.get("min_samples", 6)) and max(vals) == min(vals):
                bad += 1
                self._raise(c, sid, self._fmt(c.get("title", "{name}: unchanged ({value}) for {hours} h"),
                                              name=self._name(r), value=f"{vals[-1]:g}", hours=f"{hours:g}"),
                            f"{len(vals)} samples, all {vals[-1]:g}", sid)
            elif len(vals) >= 2:
                self._resolve(c, sid)
        return f"{bad} of {len(rows)} stuck"

    def _check_identify(self, c: dict) -> str:
        """Re-identify the mixer model from live data; proposals and drift alerts follow (no LLM)."""
        job = self.service.parameters.start_identification()
        return f"identification started {job['started']}"

    def _check_agent(self, c: dict) -> str:
        agents = self.service.agents
        run_id = agents.start(c["skill"], c.get("question", ""))
        self._agent_runs[c["id"]] = run_id
        return f"started run {run_id}"

    def _collect_agent_runs(self) -> None:
        """Findings of finished scheduled agent runs become alerts."""
        for cid, run_id in list(self._agent_runs.items()):
            c = next((x for x in self.checks if x["id"] == cid), None)
            meta = self.service.agents.run(run_id)["meta"]
            if meta.get("status") in ("running", "starting"):
                continue
            del self._agent_runs[cid]
            if c is None:
                continue
            st = self.state.setdefault(cid, {})
            if meta.get("status") != "done":
                st["error"] = f"run {run_id}: {meta.get('status')} {meta.get('error') or ''}".strip()
                continue
            mapping = {"alert": "alarm", "warning": "warning", **(c.get("severity_map") or {})}
            n = 0
            for f in meta.get("findings") or []:
                sev = mapping.get(f.get("severity"))
                if sev in SEVERITIES and sev != "info":
                    n += 1
                    self._raise(c, f.get("title", "finding")[:80], f"{f.get('title')} (run {run_id})",
                                f.get("evidence", ""), severity=sev)
            st["result"] = f"run {run_id}: {n} findings raised"

    def tick(self) -> None:
        now = time.time()
        for c in list(self.checks):
            if c.get("enabled") is False:
                continue
            if c["type"] == "event":
                after = c.get("clear_after")
                if after:
                    for key in self.store.open_keys(c["id"]):
                        a = self.store.alerts.get(key)
                        if a and now - a["last"] > duration_s(after) and self.store.resolve(key):
                            self.publish()
                continue
            if c["type"] == "agent" and c["id"] in self._agent_runs:
                continue
            if self.due(c, now):
                self.run_check(c)
        if self._agent_runs:
            self._collect_agent_runs()
        params = getattr(self.service, "_parameters", None)
        if params is not None:
            params.check_timeouts()

    def run_now(self, check_id: str) -> dict:
        c = next((x for x in self.checks if x["id"] == check_id), None)
        if c is None:
            raise KeyError(check_id)
        if c["type"] == "event":
            raise ValueError("event checks run on incoming messages")
        return self.run_check(c)

    # --- lifecycle and views ---------------------------------------------------------------
    def start(self) -> None:
        def loop():
            if self._stop.wait(20):                      # let the catalog fill after a start
                return
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("supervision tick failed")
                self._stop.wait(15)

        threading.Thread(target=loop, name="supervision", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def view(self) -> dict:
        alerts = self.store.view()
        checks = []
        for c in self.checks:
            st = self.state.get(c["id"], {})
            checks.append({"id": c["id"], "type": c["type"], "enabled": c.get("enabled", True) is not False,
                           "schedule": c.get("cron") or (f"every {c['every']}" if c.get("every") else "on message"),
                           "severity": c.get("severity", "warning"), "last_run": st.get("last_run"),
                           "result": st.get("result"), "error": st.get("error"),
                           "open": len(self.store.open_keys(c["id"]))})
        return {"alerts": alerts, "checks": checks, "error": self.error, "checks_path": self.checks_path,
                "mqtt": {"publish": self.publish_enabled, "topic": f"{self.topic}/alerts"}}

    def summary(self) -> dict:
        open_ = [a for a in self.store.view() if a["state"] != "resolved"]
        return {"open": len(open_), "alarms": sum(a["severity"] == "alarm" for a in open_),
                "warnings": sum(a["severity"] == "warning" for a in open_)}

    # --- MQTT / Home Assistant ------------------------------------------------------------
    def publish(self) -> None:
        mqtt = getattr(self.service, "mqtt", None)
        if not (self.publish_enabled and mqtt):
            return
        open_ = [a for a in self.store.view() if a["state"] != "resolved"]
        payload = {**self.summary(), "items": [
            {"title": a["title"], "severity": a["severity"], "state": a["state"],
             "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(a["first"])), "count": a["count"]}
            for a in open_[:20]]}
        mqtt.publish(f"{self.topic}/alerts", json.dumps(payload, ensure_ascii=False), retain=True)

    def publish_discovery(self) -> None:
        """Home Assistant entities: a problem sensor and the alert count, with the observer's availability."""
        mqtt = getattr(self.service, "mqtt", None)
        if not (self.publish_enabled and mqtt):
            return
        device = {"identifiers": ["heatpumpsn_observer"], "name": "HeatpumpsN observer", "manufacturer": "HeatpumpsN"}
        avail = {"availability_topic": f"{self.topic}/status", "payload_available": "online",
                 "payload_not_available": "offline"}
        mqtt.publish(f"{self.topic}/status", "online", retain=True)
        mqtt.publish(f"{self.discovery}/binary_sensor/heatpumpsn_observer/problem/config", json.dumps({
            "name": "Heating problem", "unique_id": "heatpumpsn_observer_problem", "device_class": "problem",
            "state_topic": f"{self.topic}/alerts", "value_template": "{{ 'ON' if value_json.open > 0 else 'OFF' }}",
            "json_attributes_topic": f"{self.topic}/alerts", "device": device, **avail}), retain=True)
        mqtt.publish(f"{self.discovery}/sensor/heatpumpsn_observer/alerts/config", json.dumps({
            "name": "Heating alerts", "unique_id": "heatpumpsn_observer_alerts", "icon": "mdi:alert",
            "state_topic": f"{self.topic}/alerts", "value_template": "{{ value_json.open }}",
            "json_attributes_topic": f"{self.topic}/alerts", "device": device, **avail}), retain=True)
        self.publish()
