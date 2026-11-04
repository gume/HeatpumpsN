"""The portal's log file: <data directory>/logs/observer.log (rotating), and reading it back.

Everything the portal logs goes there - warnings and errors of every module,
failed requests with their message, LLM failures - so a message that flashed
by on the page can be read again (Settings → Log, GET /api/log). The data
collector logs to its container's output (`docker compose logs collector`).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ([\w.]+): (.*)$")


def path(cfg: dict) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg["config_path"])), "logs", "observer.log")


def setup(cfg: dict, max_bytes: int = 2_000_000, backups: int = 5) -> str:
    """Attach the rotating file to the root logger (idempotent)."""
    p = path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(h, "baseFilename", None) == p for h in root.handlers):
        h = logging.handlers.RotatingFileHandler(p, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        h.setFormatter(logging.Formatter(FORMAT))
        h.setLevel(logging.INFO)
        root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return p


def read(cfg: dict, lines: int = 300, level: str = "", text: str = "", max_bytes: int = 3_000_000) -> dict:
    """The newest entries (a traceback stays with its entry), oldest first; level = the lowest shown."""
    p = path(cfg)
    files = [f for f in (p + ".1", p) if os.path.exists(f)]
    raw = ""
    for f in files:
        with open(f, "rb") as fp:
            fp.seek(max(0, os.path.getsize(f) - max_bytes))
            raw += fp.read().decode("utf-8", "replace")
    raw = raw[-max_bytes:]
    entries: list[dict] = []
    for line in raw.splitlines():
        m = LINE.match(line)
        if m:
            entries.append({"time": m.group(1), "level": m.group(2), "name": m.group(3), "message": m.group(4)})
        elif entries and line.strip():
            entries[-1]["message"] += "\n" + line
    lowest = LEVELS.index(level.upper()) if level.upper() in LEVELS else 0
    needle = text.lower().strip()
    entries = [e for e in entries if LEVELS.index(e["level"]) >= lowest
               and (not needle or needle in e["message"].lower() or needle in e["name"].lower())]
    return {"path": p, "entries": entries[-lines:], "matching": len(entries)}
