"""The observer as one long-running service: MQTT ingest plus the web portal.

Everything shared between the MQTT thread and web requests (catalog,
registry, counters) is guarded by one lock. Registry edits from the portal
are written to registry.yaml and take effect for the next message.
"""
from __future__ import annotations

import logging
import os
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
        self.settings = Settings(paths["settings"], float((cfg.get("llm") or {}).get("timeout_s", 300)))
        self.started = time.time()
        self._agents = None
        self._stop = threading.Event()
        self._saver: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------------
    def start(self, mqtt_args: dict | None) -> None:
        if mqtt_args:
            self.mqtt = MqttRunner(self._handle, **mqtt_args)
            self.mqtt.start()
        self._saver = threading.Thread(target=self._save_loop, name="catalog-saver", daemon=True)
        self._saver.start()

    def stop(self) -> None:
        self._stop.set()
        if self.mqtt:
            self.mqtt.stop()
        with self.lock:
            self.ingest.catalog.save()
        self.writer.close()

    def _handle(self, topic: str, payload: bytes, retained: bool) -> None:
        with self.lock:
            self.ingest.handle(topic, payload, retained)

    def _save_loop(self) -> None:
        while not self._stop.wait(60):
            with self.lock:
                self.ingest.catalog.save()

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
            return {
                "uptime_s": round(now - self.started),
                "mqtt": {"connected": bool(self.mqtt and self.mqtt.connected),
                         "host": self.mqtt.host if self.mqtt else None,
                         "port": self.mqtt.port if self.mqtt else None,
                         "messages": self.mqtt.messages if self.mqtt else 0,
                         "last_message_age_s": round(now - self.mqtt.last_message)
                         if self.mqtt and self.mqtt.last_message else None},
                "storage": {"kind": self.writer_kind, "samples": self.ingest.stored},
                "signals": {"total": len(self.ingest.catalog.entries), "stored": stored_signals,
                            "unregistered": unregistered},
                "sources": dict(sorted(sources.items())),
                "llm": {t["task"]: t["ready"] for t in self.settings.view()["tasks"]},
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

    # --- the reviewed house description -----------------------------------------------
    # observer/house/ (in git): description.md and model.yaml (the audited
    # version), notes.md (the owner's review notes, always editable),
    # archive/<time>/ (every audited version with the notes of that moment).
    # The proposal waits in data/house_proposal.yaml until it is accepted.
    def _house(self, name: str) -> str:
        return os.path.join(self.cfg["paths"]["house_dir"], name)

    def _read(self, path: str) -> str:
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""

    def house(self) -> dict:
        archive_dir = self._house("archive")
        archive = sorted(os.listdir(archive_dir), reverse=True) if os.path.isdir(archive_dir) else []
        proposal = None
        if os.path.exists(self.cfg["paths"]["house_proposal"]):
            with open(self.cfg["paths"]["house_proposal"], encoding="utf-8") as fp:
                proposal = yaml.safe_load(fp)
        model_text = self._read(self._house("model.yaml"))
        return {"current": {"description": self._read(self._house("description.md")),
                            "model": yaml.safe_load(model_text) if model_text else None,
                            "audited": archive[0] if archive else None},
                "proposal": proposal, "notes": self._read(self._house("notes.md")), "archive": archive}

    def save_notes(self, text: str) -> None:
        os.makedirs(self.cfg["paths"]["house_dir"], exist_ok=True)
        with open(self._house("notes.md"), "w", encoding="utf-8") as fp:
            fp.write(text.rstrip() + "\n" if text.strip() else "")

    def house_inputs(self) -> tuple[dict, dict]:
        """(described signals, candidates): what the description is built from."""
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

    def generate_house(self, client=None) -> dict:
        from .llm import build_house_description

        client = client or self.settings.client("house")
        if client is None:
            raise RuntimeError("no LLM configured for the house description (Settings)")
        described, candidates = self.house_inputs()
        out = build_house_description(client, self.cfg.get("context", ""), described, candidates,
                                      self._read(self._house("notes.md")), self._read(self._house("description.md")))
        out["generated"] = time.strftime("%Y-%m-%d %H:%M")
        out["model_name"] = getattr(client, "model", "")
        with open(self.cfg["paths"]["house_proposal"], "w", encoding="utf-8") as fp:
            yaml.safe_dump(out, fp, sort_keys=False, allow_unicode=True, width=100)
        return out

    def edit_proposal(self, description: str) -> None:
        path = self.cfg["paths"]["house_proposal"]
        if not os.path.exists(path):
            raise FileNotFoundError("no proposal")
        with open(path, encoding="utf-8") as fp:
            proposal = yaml.safe_load(fp)
        proposal["description_md"] = description
        proposal["edited"] = True
        with open(path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(proposal, fp, sort_keys=False, allow_unicode=True, width=100)

    def accept_house(self) -> str:
        """The proposal becomes the audited version; it is archived with the current notes."""
        path = self.cfg["paths"]["house_proposal"]
        if not os.path.exists(path):
            raise FileNotFoundError("no proposal")
        with open(path, encoding="utf-8") as fp:
            proposal = yaml.safe_load(fp)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        folder = self._house(os.path.join("archive", stamp))
        os.makedirs(folder, exist_ok=True)
        model_yaml = yaml.safe_dump(proposal.get("model") or {}, sort_keys=False, allow_unicode=True, width=100)
        review = {k: proposal.get(k) for k in ("generated", "model_name", "edited", "questions", "recommendations")}
        for target in (self.cfg["paths"]["house_dir"], folder):
            with open(os.path.join(target, "description.md"), "w", encoding="utf-8") as fp:
                fp.write(proposal["description_md"].rstrip() + "\n")
            with open(os.path.join(target, "model.yaml"), "w", encoding="utf-8") as fp:
                fp.write(model_yaml)
        with open(os.path.join(folder, "review.yaml"), "w", encoding="utf-8") as fp:
            yaml.safe_dump(review, fp, sort_keys=False, allow_unicode=True, width=100)
        with open(os.path.join(folder, "notes.md"), "w", encoding="utf-8") as fp:
            fp.write(self._read(self._house("notes.md")))
        os.remove(path)
        return stamp

    def archived(self, stamp: str) -> dict:
        if "/" in stamp or ".." in stamp:
            raise KeyError(stamp)
        folder = self._house(os.path.join("archive", stamp))
        if not os.path.isdir(folder):
            raise KeyError(stamp)
        review = self._read(os.path.join(folder, "review.yaml"))
        return {"stamp": stamp, "description": self._read(os.path.join(folder, "description.md")),
                "notes": self._read(os.path.join(folder, "notes.md")),
                "review": yaml.safe_load(review) if review else {}}
