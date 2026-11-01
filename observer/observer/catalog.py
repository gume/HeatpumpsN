"""Every signal the observer has seen, kept up to date automatically.

The catalog is the observer's inventory: when a signal was first and last
seen, how often, its last value, whether it is numeric, and the metadata the
stream carries about it. Sensors that come and go show up here first; the
registry (registry.py) decides what is stored and what it means.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Entry:
    first_seen: float
    last_seen: float
    count: int = 0
    last_value: float | None = None
    last_text: str | None = None
    numeric: bool = True
    meta: dict = field(default_factory=dict)


class Catalog:
    def __init__(self, path: str | None = None):
        self.path = path
        self.entries: dict[str, Entry] = {}
        self._pending_meta: dict[str, dict] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                for sig, data in json.load(fp).items():
                    self.entries[sig] = Entry(**data)

    def observe(self, signal: str, value: float | None, text: str | None, now: float | None = None) -> bool:
        """Record one sample; True when the signal is new."""
        now = time.time() if now is None else now
        entry = self.entries.get(signal)
        new = entry is None
        if new:
            entry = self.entries[signal] = Entry(first_seen=now, last_seen=now)
            entry.meta.update(self._pending_meta.pop(signal, {}))
        entry.last_seen = now
        entry.count += 1
        entry.last_value, entry.last_text = value, text
        if text is not None:
            entry.numeric = False
        return new

    def add_meta(self, metadata: dict[str, dict]) -> None:
        """Merge stream metadata; kept for signals not seen yet."""
        for sig, meta in metadata.items():
            if sig in self.entries:
                self.entries[sig].meta.update(meta)
            else:
                self._pending_meta.setdefault(sig, {}).update(meta)

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump({sig: asdict(e) for sig, e in sorted(self.entries.items())}, fp, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)
