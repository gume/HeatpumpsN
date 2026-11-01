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

    # --- LLM: polishing one description -------------------------------------------
    def polish(self, signal: str, draft: dict, client=None) -> dict:
        from .llm import LlmClient, polish

        client = client or LlmClient.from_config(self.cfg.get("llm"))
        if client is None:
            raise RuntimeError("no LLM configured (OBSERVER_LLM_BASE_URL and OBSERVER_LLM_MODEL)")
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

    # --- the house model ------------------------------------------------------------
    def _model_paths(self) -> tuple[str, str]:
        return self.cfg["paths"]["house_model"], self.cfg["paths"]["house_model_proposal"]

    def model(self) -> dict:
        current, proposal = self._model_paths()
        out = {}
        for key, path in (("current", current), ("proposal", proposal)):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fp:
                    out[key] = yaml.safe_load(fp) or {}
                out[f"{key}_text"] = open(path, encoding="utf-8").read()
            else:
                out[key] = None
        return out

    def model_inputs(self) -> dict[str, dict]:
        """Described, stored, non-diagnostic signals: what the model is built from."""
        with self.lock:
            signals = {}
            for sig in self.ingest.catalog.entries:
                r = self.ingest.registry.resolve(sig)
                if r.store and r.info.get("role") != "diagnostic" and (r.info.get("name") or r.info.get("description")):
                    signals[sig] = r.info
        return signals

    def generate_model(self, client=None) -> dict:
        from .llm import LlmClient, build_house_model

        client = client or LlmClient.from_config(self.cfg.get("llm"))
        if client is None:
            raise RuntimeError("no LLM configured (OBSERVER_LLM_BASE_URL and OBSERVER_LLM_MODEL)")
        model = build_house_model(client, self.model_inputs(), self.cfg.get("context", ""))
        model["generated"] = time.strftime("%Y-%m-%d %H:%M")
        with open(self._model_paths()[1], "w", encoding="utf-8") as fp:
            yaml.safe_dump(model, fp, sort_keys=False, allow_unicode=True, width=100)
        return model

    def accept_model(self) -> None:
        current, proposal = self._model_paths()
        if not os.path.exists(proposal):
            raise FileNotFoundError("no proposal")
        os.replace(proposal, current)

    def save_model_text(self, text: str) -> None:
        data = yaml.safe_load(text)          # raises on invalid YAML
        if not isinstance(data, dict):
            raise ValueError("the house model must be a YAML mapping")
        with open(self._model_paths()[0], "w", encoding="utf-8") as fp:
            fp.write(text)
