"""Move an observer's state (not its recorded data) to another machine.

The state is what makes this observer know this house: the registry (which
signals are stored and what they mean), the catalog, the house
understanding (review, owner knowledge, accepted versions, archive, draft),
the house part of the library (items, history, packs, audit flags, test
results), the per-task model settings and the house context from
config.yaml. Not included: secrets (.env), machine-specific settings
(config.yaml apart from the context), agent runs, database exports, the
sandbox environment. The recorded samples move with the database
export/import (dbarchive.py).

Import replaces these files; the replaced ones are kept in
``<OBSERVER_HOME>/backup/<time>/``.
"""
from __future__ import annotations

import io
import os
import shutil
import tarfile
import time

import yaml

FORMAT = "heatpumpsn-observer-state/1"
# (archive path, path relative to the data directory); folders end with "/".
ITEMS = (("registry.yaml", "registry"), ("catalog.json", "catalog"), ("settings.yaml", "settings"),
         ("house_draft.yaml", "house_draft"), ("house/", "house_dir"), ("library/", "library"))
LIBRARY_SKIP = ("tests_tmp",)
MAX_ARCHIVE = 200 * 1024 * 1024


def _files(root: str):
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") or d == ".previous")
        for n in sorted(names):
            if not n.endswith((".tmp", ".pyc")):
                yield os.path.join(base, n)


def export_state(cfg: dict) -> bytes:
    paths = cfg["paths"]
    buf = io.BytesIO()
    included = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(arc: str, data: bytes) -> None:
            info = tarfile.TarInfo(arc)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))

        for arc, key in ITEMS:
            src = paths[key]
            if arc.endswith("/"):
                if os.path.isdir(src):
                    for f in _files(src):
                        add(arc + os.path.relpath(f, src), open(f, "rb").read())
                    included.append(arc)
            elif os.path.isfile(src):
                add(arc, open(src, "rb").read())
                included.append(arc)
        add("context.md", (cfg.get("context") or "").encode())
        add("state.yaml", yaml.safe_dump({"format": FORMAT, "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "included": included}, sort_keys=False).encode())
    return buf.getvalue()


def _target(cfg: dict, name: str) -> str | None:
    """Where an archive member goes, or None if it is not part of a state."""
    for arc, key in ITEMS:
        if arc.endswith("/") and name.startswith(arc):
            return os.path.join(cfg["paths"][key], name[len(arc):])
        if name == arc:
            return cfg["paths"][key]
    return None


def import_state(cfg: dict, data: bytes, config_path: str) -> dict:
    if len(data) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ValueError(f"not a tar/tgz archive: {exc}") from exc
    members, context, manifest = {}, None, None
    with tar:
        for m in tar.getmembers():
            if m.isdir():
                continue
            name = m.name[2:] if m.name.startswith("./") else m.name
            parts = name.split("/")
            if not m.isfile() or name.startswith("/") or any(p in ("", ".", "..") for p in parts):
                raise ValueError(f"{m.name}: unsafe entry")
            content = tar.extractfile(m).read()
            if name == "state.yaml":
                manifest = yaml.safe_load(content) or {}
            elif name == "context.md":
                context = content.decode("utf-8")
            elif _target(cfg, name) is None:
                raise ValueError(f"{m.name}: not part of an observer state")
            else:
                members[name] = content
    if not manifest or manifest.get("format") != FORMAT:
        raise ValueError("not an observer state archive (state.yaml missing or another format)")
    home = os.path.dirname(os.path.abspath(config_path))
    backup = os.path.join(home, "backup", time.strftime("%Y%m%d-%H%M%S"))
    for arc, key in ITEMS:                         # move what will be replaced out of the way
        if not any(n == arc or (arc.endswith("/") and n.startswith(arc)) for n in members):
            continue
        src = cfg["paths"][key]
        if os.path.exists(src):
            os.makedirs(backup, exist_ok=True)
            shutil.move(src, os.path.join(backup, arc.rstrip("/")))
    for name, content in members.items():
        target = _target(cfg, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fp:
            fp.write(content)
    if context is not None and context.strip():
        _set_context(config_path, context)
        cfg["context"] = context
    return {"exported": manifest.get("exported"), "files": len(members), "context": bool(context and context.strip()),
            "backup": backup if os.path.isdir(backup) else None}


def _set_context(config_path: str, context: str) -> None:
    """Replace the `context` of config.yaml, keeping its comments and layout."""
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import LiteralScalarString

    y = YAML()
    y.preserve_quotes = True
    with open(config_path, encoding="utf-8") as fp:
        doc = y.load(fp)
    doc["context"] = LiteralScalarString(context.rstrip() + "\n")
    with open(config_path, "w", encoding="utf-8") as fp:
        y.dump(doc, fp)
