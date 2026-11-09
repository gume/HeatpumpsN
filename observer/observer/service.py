"""The observer as one long-running service: MQTT ingest plus the web portal.

Everything shared between the MQTT thread and web requests (catalog,
registry, counters) is guarded by one lock. Registry edits from the portal
are written to registry.yaml and take effect for the next message.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import yaml

from .catalog import Catalog
from .ingest import Ingest, MqttRunner
from .registry import FIELDS, Registry

log = logging.getLogger(__name__)


# HA device classes that name a registry quantity directly.
_DEVICE_CLASS_QUANTITY = {"temperature": "temperature", "humidity": "humidity", "power": "power",
                          "energy": "energy", "battery": "battery", "illuminance": "illuminance",
                          "signal_strength": "signal_strength", "pressure": "pressure",
                          "motion": "motion", "occupancy": "motion", "door": "contact", "window": "contact",
                          "running": "state", "heat": "state"}


def effective(info: dict, meta: dict) -> dict:
    """Unit, quantity and device: the registry's when set, else what the stream says."""
    return {"unit": info.get("unit") or meta.get("unit") or "",
            "quantity": info.get("quantity") or _DEVICE_CLASS_QUANTITY.get(meta.get("device_class", ""), ""),
            "device": meta.get("device") or ""}


class Service:
    def __init__(self, cfg: dict, writer, writer_kind: str):
        self.cfg = cfg
        self.writer, self.writer_kind = writer, writer_kind
        self.lock = threading.RLock()
        paths = cfg["paths"]
        m = cfg.get("mqtt", {})
        self.ingest = Ingest(Catalog(paths["catalog"]), Registry(paths["registry"]), writer,
                             prefix=m.get("topic_prefix", ""),
                             statestream_base=cfg.get("statestream_base", "ha_state"))
        self.mqtt: MqttRunner | None = None
        from .settings import Settings
        self.settings = Settings(paths["settings"])
        self.started = time.time()
        self._agents = None
        self._supervisor = None
        self._parameters = None
        self.db_total: dict | None = None        # samples in the database, counted once at the start
        self._last_total: int | None = None
        self._db_job: dict | None = None
        from . import __version__
        from .updates import UpdateChecker

        def library():
            from .agent.manager import make_library
            return self._agents.library if self._agents else make_library(self.cfg)

        self.updates = UpdateChecker(cfg, __version__, library)
        self._stop = threading.Event()
        self._saver: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------------
    @property
    def supervisor(self):
        """Automatic checks and alerts (created on first use)."""
        if self._supervisor is None:
            from .supervision import Supervisor
            self._supervisor = Supervisor(self, self.cfg)
        return self._supervisor

    @property
    def parameters(self):
        """Mixer-model identification and parameter proposals (created on first use)."""
        if self._parameters is None:
            from .parameters import ParameterManager
            self._parameters = ParameterManager(self, self.cfg)
        return self._parameters

    def start(self, mqtt_args: dict | None) -> None:
        sup = self.supervisor
        self.ingest.listeners.append(sup.on_message)
        self.ingest.raw_listeners.append(self.parameters.on_raw)
        self.ingest.ignore = (sup.topic + "/",)            # the observer's own publications
        if mqtt_args:
            self.mqtt = MqttRunner(self._handle, **mqtt_args, will=(f"{sup.topic}/status", "offline"))
            self.mqtt.on_connect_hooks.append(sup.publish_discovery)
            self.mqtt.start()
        self._saver = threading.Thread(target=self._save_loop, name="catalog-saver", daemon=True)
        self._saver.start()
        self.updates.start()
        sup.start()

    def stop(self) -> None:
        self._stop.set()
        self.updates.stop()
        if self._supervisor:
            self._supervisor.stop()
        if self.mqtt:
            self.mqtt.stop()
        with self.lock:
            self.ingest.catalog.save()
        self.writer.close()

    def _handle(self, topic: str, payload: bytes, retained: bool) -> None:
        with self.lock:
            self.ingest.handle(topic, payload, retained)

    def _save_loop(self) -> None:
        self._count_total()
        while not self._stop.wait(60):
            with self.lock:
                self.ingest.catalog.save()

    def collector_status(self) -> dict | None:
        """The separate collector's state (data/collector.json), when the ingest is external."""
        if self.writer_kind != "external":
            return None
        from .collector import read_status
        return read_status(self.cfg)

    def _written(self) -> tuple:
        """(session, samples written in it): this process, or the separate collector's run."""
        if self.writer_kind == "external":
            st = self.collector_status() or {}
            return st.get("started"), int(st.get("stored") or 0)
        return "local", self.ingest.stored

    def stored_total(self) -> int | None:
        """The database total: counted once, plus what was written since (a new collector run rebases)."""
        if not self.db_total:
            return None
        session, stored = self._written()
        if session != self.db_total["session"]:      # the collector restarted: its counter began at 0
            self.db_total = {"samples": self._last_total or self.db_total["samples"], "session": session,
                             "stored_then": 0, "counted": self.db_total.get("counted")}
        total = self.db_total["samples"] + stored - self.db_total["stored_then"]
        self._last_total = total
        return total

    def recount(self) -> dict:
        """Count the database again (button on the Status tab; after every import)."""
        threading.Thread(target=self._count_total, name="count", daemon=True).start()
        return {"ok": True}

    def _count_total(self) -> None:
        """Count the samples in the database (at the start and on request); the status adds what was written since."""
        if self.writer_kind not in ("influx", "external"):
            return
        try:
            import tempfile

            influx = self._influx(write=False)
            with tempfile.TemporaryDirectory(prefix="obs-count-") as tmp:
                p = os.path.join(tmp, "c.csv")
                influx.query_csv(f'from(bucket: "{influx.bucket}") |> range(start: 0)\n'
                                 '  |> filter(fn: (r) => r._measurement == "signal")\n'
                                 '  |> count() |> group() |> sum()\n'
                                 '  |> map(fn: (r) => ({t: r._value}))', p)
                with open(p, encoding="utf-8") as fp:
                    lines = [ln.strip() for ln in fp if ln.strip()]
                # ",result,table,t" then ",_result,0,<count>"; an empty bucket gives no lines
                total = int(lines[1].split(",")[-1]) if len(lines) > 1 else 0
            session, stored = self._written()
            self.db_total = {"samples": total, "session": session, "stored_then": stored,
                             "counted": time.strftime("%Y-%m-%d %H:%M:%S")}
        except Exception as exc:              # shown as unknown; ingest is unaffected
            log.warning("counting the stored samples failed: %s", exc)

    # --- the observer's state (registry, house, library, settings) -----------------
    def state_export(self) -> bytes:
        from .state import export_state

        with self.lock:
            self.ingest.catalog.save()
        return export_state(self.cfg)

    def state_import(self, data: bytes) -> dict:
        """Replace registry, catalog, house, library and settings; the old ones go to backup/."""
        from .registry import Registry
        from .catalog import Catalog
        from .settings import Settings
        from .state import import_state

        with self.lock:
            result = import_state(self.cfg, data, self.cfg["config_path"])
            paths = self.cfg["paths"]
            self.ingest.registry = Registry(paths["registry"])
            self.ingest.catalog = Catalog(paths["catalog"])
            self.settings = Settings(paths["settings"])
            self._agents = None                      # the library is read again on first use
        return result

    # --- database export / import (background jobs) ---------------------------------
    def _influx(self, write: bool):
        from .dbarchive import Influx

        i = self.cfg.get("influx") or {}
        token = os.environ.get(i.get("token_env", "INFLUX_TOKEN"), "") if write else (
            os.environ.get(((self.cfg.get("agent") or {}).get("read_token_env") or "OBSERVER_INFLUX_READ_TOKEN"), "")
            or os.environ.get(i.get("token_env", "INFLUX_TOKEN"), ""))
        if not token or not i.get("url"):
            raise RuntimeError("no InfluxDB configured (influx.url and the token in .env)")
        return Influx(i["url"], i.get("org", ""), i.get("bucket", ""), token)

    def db_files(self) -> list[dict]:
        d = self.cfg["paths"]["exports"]
        out = []
        for name in sorted(os.listdir(d), reverse=True):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.endswith(".parquet"):
                out.append({"name": name, "bytes": os.path.getsize(p),
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))})
        return out

    def db_file(self, name: str) -> str:
        path = os.path.join(self.cfg["paths"]["exports"], os.path.basename(name))
        if not name.endswith(".parquet") or not os.path.isfile(path):
            raise KeyError(name)
        return path

    def db_status(self) -> dict:
        return {"job": self._db_job, "files": self.db_files()}

    def _start_db_job(self, kind: str, work) -> dict:
        with self.lock:
            if self._db_job and self._db_job.get("state") == "running":
                raise RuntimeError(f"a database {self._db_job['kind']} is still running")
            job = {"kind": kind, "state": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S"), "progress": ""}
            self._db_job = job

        def run():
            try:
                job["result"] = work(lambda msg: job.update(progress=msg))
                job["state"] = "done"
            except Exception as exc:             # reported to the portal
                job.update(state="error", error=f"{type(exc).__name__}: {exc}")
            job["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")

        threading.Thread(target=run, name=f"db-{kind}", daemon=True).start()
        return job

    def db_export(self, start: str | None = None, stop: str | None = None) -> dict:
        """Export the recorded samples (read-only token) into exports/observer-<time>.parquet."""
        from .dbarchive import export_db

        influx = self._influx(write=False)
        name = time.strftime("observer-%Y%m%d-%H%M%S.parquet")
        path = os.path.join(self.cfg["paths"]["exports"], name)
        signals = self.signals()
        return self._start_db_job("export", lambda progress: export_db(influx, path, start or None, stop or None,
                                                                         signals, progress))

    def db_import(self, path: str, dry_run: bool = False) -> dict:
        """Add the samples of an export file that the database does not have; never deletes anything."""
        from .dbarchive import import_db, read_meta

        read_meta(path)                                   # reject a wrong file before starting
        influx = self._influx(write=True)
        def work(progress):
            result = import_db(influx, path, dry_run, progress)
            if not dry_run and result.get("written"):
                self._count_total()                  # the import bypasses the counter: count again
            return result

        return self._start_db_job("import", work)

    @property
    def agents(self):
        """Agent runs (created on first use: database client and sandbox)."""
        if self._agents is None:
            from .agent.manager import AgentManager
            self._agents = AgentManager(self, self.cfg)
        return self._agents

    # --- views -------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        with self.lock:
            sources: dict[str, dict] = {}
            unregistered = stored_signals = 0
            for sig, entry in self.ingest.catalog.entries.items():
                src = sig.split("/", 1)[0]
                s = sources.setdefault(src, {"signals": 0, "stored": 0, "last_seen": 0.0})
                s["signals"] += 1
                s["last_seen"] = max(s["last_seen"], entry.last_seen)
                resolved = self.ingest.registry.resolve(sig)
                if resolved.store:
                    s["stored"] += 1
                    stored_signals += 1
                if resolved.source == "none":
                    unregistered += 1
            for s in sources.values():
                s["age_s"] = round(now - s["last_seen"]) if s["last_seen"] else None
            from . import __version__

            return {
                "version": __version__,
                "uptime_s": round(now - self.started),
                "mqtt": {"connected": bool(self.mqtt and self.mqtt.connected),
                         "host": self.mqtt.host if self.mqtt else None,
                         "port": self.mqtt.port if self.mqtt else None,
                         "messages": self.mqtt.messages if self.mqtt else 0,
                         "last_message_age_s": round(now - self.mqtt.last_message)
                         if self.mqtt and self.mqtt.last_message else None},
                "storage": {"kind": self.writer_kind, "samples": self._written()[1],
                            "total": self.stored_total(),
                            "counted": self.db_total.get("counted") if self.db_total else None,
                            "collector": self.collector_status()},
                "signals": {"total": len(self.ingest.catalog.entries), "stored": stored_signals,
                            "unregistered": unregistered},
                "sources": dict(sorted(sources.items())),
                "llm": {t["task"]: t["ready"] for t in self.settings.view()["tasks"]},
                "updates": self.updates.summary()["available"],
                "alerts": self.supervisor.summary(),
            }

    def signals(self) -> list[dict]:
        now = time.time()
        rows = []
        with self.lock:
            for sig, e in sorted(self.ingest.catalog.entries.items()):
                r = self.ingest.registry.resolve(sig)
                rows.append({
                    **effective(r.info, e.meta),
                    "id": sig, "source": sig.split("/", 1)[0], "store": r.store, "resolved_by": r.source,
                    "info": r.info, "entry": self.ingest.registry.signals.get(sig),
                    "meta": e.meta, "numeric": e.numeric, "count": e.count,
                    "value": e.last_value, "text": e.last_text,
                    "age_s": round(now - e.last_seen),
                })
        return rows

    def rules(self) -> list[dict]:
        with self.lock:
            return list(self.ingest.registry.rules)

    # --- registry edits -------------------------------------------------------------
    def set_signal(self, signal: str, entry: dict) -> dict:
        clean = {k: str(entry[k]).strip() for k in FIELDS if entry.get(k) not in (None, "")}
        clean["store"] = bool(entry.get("store", True))
        with self.lock:
            self.ingest.registry.add(signal, clean)
            self.ingest.registry.save()
            r = self.ingest.registry.resolve(signal)
        log.info("registry: %s set (store=%s)", signal, r.store)
        return {"id": signal, "store": r.store, "info": r.info, "resolved_by": r.source}

    def update_signals(self, signals: list[str], fields: dict) -> int:
        """Set the given fields on several signals, keeping everything else of their entries."""
        allowed = {k: v for k, v in fields.items() if k in FIELDS + ("store",)}
        if not allowed:
            return 0
        with self.lock:
            for sig in signals:
                entry = dict(self.ingest.registry.signals.get(sig) or {})
                for k, v in allowed.items():
                    if k == "store":
                        entry["store"] = bool(v)
                    elif v in (None, ""):
                        entry.pop(k, None)
                    else:
                        entry[k] = str(v).strip()
                self.ingest.registry.add(sig, entry)
            self.ingest.registry.save()
        log.info("registry: %d signals updated (%s)", len(signals), ", ".join(allowed))
        return len(signals)

    def remove_signal(self, signal: str) -> bool:
        with self.lock:
            removed = self.ingest.registry.remove(signal)
            if removed:
                self.ingest.registry.save()
        return removed

    # --- LLM proposals ----------------------------------------------------------------
    def proposals(self) -> list[dict]:
        path = self.cfg["paths"]["proposals"]
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fp:
            return (yaml.safe_load(fp) or {}).get("proposals", [])

    def _save_proposals(self, proposals: list[dict]) -> None:
        with open(self.cfg["paths"]["proposals"], "w", encoding="utf-8") as fp:
            yaml.safe_dump({"proposals": proposals}, fp, sort_keys=False, allow_unicode=True, width=100)

    def run_proposals(self, limit: int, client=None) -> int:
        """Ask the LLM about up to `limit` undescribed signals (blocking; minutes for many)."""
        from .llm import propose

        client = client or self.settings.client("propose")
        if client is None:
            raise RuntimeError("no LLM configured for bulk proposals (Settings)")
        with self.lock:
            todo = {}
            for sig, e in sorted(self.ingest.catalog.entries.items()):
                if self.ingest.registry.resolve(sig).source == "none":
                    todo[sig] = {"meta": e.meta, "last": e.last_text if e.last_value is None else e.last_value,
                                 "numeric": e.numeric, "messages": e.count}
                if len(todo) >= limit:
                    break
            known = {s: {k: v for k, v in entry.items() if k != "store"}
                     for s, entry in self.ingest.registry.signals.items()}
        if not todo:
            return 0
        fresh = propose(client, todo, self.cfg.get("context", ""), known)
        merged = {p["signal"]: p for p in self.proposals()}
        merged.update({p["signal"]: p for p in fresh})
        self._save_proposals(list(merged.values()))
        return len(fresh)

    def accept_proposal(self, signal: str, edits: dict | None = None) -> dict:
        proposals = self.proposals()
        match = next((p for p in proposals if p["signal"] == signal), None)
        if match is None:
            raise KeyError(signal)
        result = self.set_signal(signal, {**match, **(edits or {})})
        self._save_proposals([p for p in proposals if p["signal"] != signal])
        return result

    def reject_proposal(self, signal: str) -> bool:
        proposals = self.proposals()
        rest = [p for p in proposals if p["signal"] != signal]
        self._save_proposals(rest)
        return len(rest) != len(proposals)

    # --- LLM: polishing one description -------------------------------------------
    def polish(self, signal: str, draft: dict, client=None) -> dict:
        from .llm import polish

        client = client or self.settings.client("describe")
        if client is None:
            raise RuntimeError("no LLM configured for descriptions (Settings)")
        with self.lock:
            entry = self.ingest.catalog.entries.get(signal)
            resolved = self.ingest.registry.resolve(signal)
            facts = {"stream_metadata": entry.meta if entry else {},
                     "last_value": (entry.last_text if entry.last_value is None else entry.last_value) if entry else None,
                     "messages": entry.count if entry else 0,
                     "current_registry_description": resolved.info, "described_by": resolved.source}
            # Signals from the same topic or device give the model its neighbourhood.
            stem = signal.split("#", 1)[0].rsplit("/", 1)[0]
            related = {}
            for sig, other in self.ingest.catalog.entries.items():
                if sig != signal and sig.startswith(stem):
                    info = self.ingest.registry.resolve(sig).info
                    related[sig] = {"name": info.get("name") or other.meta.get("name"), "unit": info.get("unit") or
                                    other.meta.get("unit"), "description": info.get("description")}
                if len(related) >= 15:
                    break
        return polish(client, signal, draft, facts, self.cfg.get("context", ""), related)

    # --- the house description draft ---------------------------------------------------
    # house/ in the data directory: description.md (the current draft),
    # input.md (the owner's input, always saved), previous.md (the version
    # before the last update, for undo and as a reference in the input) and
    # model.yaml (the machine model from the last update).
    def _house(self, name: str) -> str:
        return os.path.join(self.cfg["paths"]["house_dir"], name)

    def _read(self, path: str) -> str:
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""

    def _write(self, name: str, text: str) -> None:
        os.makedirs(self.cfg["paths"]["house_dir"], exist_ok=True)
        with open(self._house(name), "w", encoding="utf-8") as fp:
            fp.write(text.rstrip() + "\n" if text.strip() else "")

    def house(self) -> dict:
        model_text = self._read(self._house("model.yaml"))
        description = self._read(self._house("description.md"))
        # notes.md was the owner's review notes in the old flow; keep reading it
        # until the owner saves input for the first time.
        user_input = self._read(self._house("input.md")) or self._read(self._house("notes.md"))
        known = set(self.ingest.catalog.entries) if hasattr(self.ingest, "catalog") else set()
        named = {m for m in re.findall(r"`([^`\s]+)`", description) if "/" in m}
        return {"description": description,
                "user_input": user_input,
                "previous": self._read(self._house("previous.md")),
                "can_undo": os.path.exists(self._house("previous.md")),
                "unknown_ids": sorted(named - known),
                "model": yaml.safe_load(model_text) if model_text else None}

    def save_user_input(self, text: str) -> None:
        self._write("input.md", text)

    def house_inputs(self) -> tuple[dict, dict]:
        """(described signals, candidates): what the understanding is built from."""
        with self.lock:
            described, candidates = {}, {}
            for sig, e in self.ingest.catalog.entries.items():
                r = self.ingest.registry.resolve(sig)
                if r.store and (r.info.get("name") or r.info.get("description")):
                    described[sig] = r.info
                elif e.numeric and r.info.get("role") != "diagnostic" and e.count > 1:
                    candidates[sig] = {"stored": r.store, "name": r.info.get("name") or e.meta.get("name"),
                                       "unit": r.info.get("unit") or e.meta.get("unit"),
                                       "device": e.meta.get("device"), "last": e.last_value}
        return described, dict(list(sorted(candidates.items()))[:200])

    def update_house(self, client=None) -> dict:
        """Merge the saved input into the current draft; keep the old version for undo."""
        from .llm import update_house_description

        client = client or self.settings.client("house")
        if client is None:
            raise RuntimeError("no LLM configured for the house description (Settings)")
        current = self._read(self._house("description.md"))
        previous = self._read(self._house("previous.md"))
        user_input = self._read(self._house("input.md")) or self._read(self._house("notes.md"))
        if not user_input.strip():
            raise RuntimeError("write what should change in your input first")
        described, candidates = self.house_inputs()
        out = update_house_description(client, self.cfg.get("context", ""), described, candidates,
                                       current, previous, user_input)
        self._write("previous.md", current)
        self._write("previous_model.yaml", self._read(self._house("model.yaml")))
        self._write("description.md", out["description_md"])
        self._write("model.yaml", yaml.safe_dump(out.get("model") or {}, sort_keys=False, allow_unicode=True,
                                                width=100))
        return out

    def clear_house(self) -> dict:
        """Clear the draft; the old version stays available for undo."""
        self._write("previous.md", self._read(self._house("description.md")))
        self._write("previous_model.yaml", self._read(self._house("model.yaml")))
        self._write("description.md", "")
        self._write("model.yaml", "")
        return self.house()

    def undo_house(self) -> dict:
        """Swap the current draft with the version before the last update or clear."""
        if not os.path.exists(self._house("previous.md")):
            raise FileNotFoundError("nothing to undo")
        current, previous = self._read(self._house("description.md")), self._read(self._house("previous.md"))
        current_model = self._read(self._house("model.yaml"))
        previous_model = self._read(self._house("previous_model.yaml"))
        self._write("description.md", previous)
        self._write("model.yaml", previous_model)
        self._write("previous.md", current)
        self._write("previous_model.yaml", current_model)
        return self.house()
