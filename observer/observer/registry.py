"""Which signals matter and what they mean: the user's selection.

``registry.yaml`` holds

* ``signals``: exact signal ids with a description (name, quantity, unit,
  location, circuit, role, free text) and ``store: true/false``;
* ``rules``: glob patterns (``ha_state/sensor/bth01_*_temperature/state``)
  that give whole families the same defaults, so a new sensor of a known kind
  is described and stored without editing the file.

The first matching rule gives the defaults; an exact entry adds to them and
overrides what it sets itself (so a room sensor entry needs only its name and
location when a rule already says "temperature, °C, room").
The observer reads this file but never writes it by itself: LLM proposals go
to a separate file and are merged only on request (``observer accept``).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

import yaml

# Description fields a signal can carry; also the tags written to InfluxDB.
FIELDS = ("name", "quantity", "unit", "location", "circuit", "role", "description")
TAG_FIELDS = ("quantity", "location", "circuit", "role")


@dataclass
class Resolved:
    signal: str
    store: bool
    info: dict      # description fields that apply
    source: str     # "signal", "rule:<pattern>" or "none"


class Registry:
    def __init__(self, path: str | None = None, data: dict | None = None):
        self.path = path
        if data is None and path and os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                data = yaml.safe_load(fp) or {}
        data = data or {}
        self.signals: dict[str, dict] = data.get("signals") or {}
        self.rules: list[dict] = data.get("rules") or []
        self.store_unregistered: bool = bool(data.get("store_unregistered", False))

    def resolve(self, signal: str) -> Resolved:
        info: dict = {}
        store = self.store_unregistered
        source = "none"
        for rule in self.rules:
            if fnmatch.fnmatchcase(signal, rule.get("match", "")):
                info = {k: rule[k] for k in FIELDS if rule.get(k) not in (None, "")}
                store = bool(rule.get("store", True))
                source = f"rule:{rule['match']}"
                break
        entry = self.signals.get(signal)
        if entry is not None:
            info.update({k: entry[k] for k in FIELDS if entry.get(k) not in (None, "")})
            store = bool(entry.get("store", True))
            source = "signal"
        return Resolved(signal, store, info, source)

    def tags(self, signal: str) -> dict[str, str]:
        info = self.resolve(signal).info
        return {k: str(info[k]) for k in TAG_FIELDS if k in info}

    def add(self, signal: str, entry: dict) -> None:
        self.signals[signal] = {k: v for k, v in entry.items() if k in FIELDS + ("store",)}

    def save(self) -> None:
        if not self.path:
            raise ValueError("registry has no path")
        with open(self.path, "w", encoding="utf-8") as fp:
            yaml.safe_dump({"store_unregistered": self.store_unregistered, "rules": self.rules,
                            "signals": dict(sorted(self.signals.items()))},
                           fp, sort_keys=False, allow_unicode=True, width=100)
