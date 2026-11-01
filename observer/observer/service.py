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
        self.started = time.time()
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
                "llm": bool(self.cfg.get("llm", {}).get("base_url") and self.cfg.get("llm", {}).get("model")),
            }

    def signals(self) -> list[dict]:
        now = time.time()
        rows = []
        with self.lock:
            for sig, e in sorted(self.ingest.catalog.entries.items()):
                r = self.ingest.registry.resolve(sig)
                rows.append({
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
        from .llm import LlmClient, propose

        client = client or LlmClient.from_config(self.cfg.get("llm"))
        if client is None:
            raise RuntimeError("no LLM configured (llm.base_url and llm.model)")
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
