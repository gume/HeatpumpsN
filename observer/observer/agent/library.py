"""The library of analysis tools and skills: sources, versions, tests, audit, export/import.

Two kinds of items:

* **tools**: code that runs in the sandbox. A folder with ``tool.yaml``
  (name, version, purpose, inputs, outputs, what data it needs), ``tool.py``
  (a ``run(...)`` function) and ``test_tool.py`` (pytest, synthetic data only);
* **skills**: recipes, a folder with ``SKILL.md`` (front matter with title,
  description, version, tools + Markdown).

Items come from **sources**, highest precedence first:

1. ``house``: ``<OBSERVER_HOME>/library/{tools,skills}``, written by agents,
   the harvest or by hand; saving a house item again bumps its version and
   keeps the previous one in ``library/history/``;
2. **library folders** named in config.yaml (``agent.library_paths``), e.g. a
   git checkout of a shared library repository (development: edits are live);
3. **packs**: archives imported into ``<OBSERVER_HOME>/library/packs/<name>/``.

A pack (and a library folder) has a ``library.yaml`` (name, version,
description). ``export()`` writes a .tgz of chosen items with such a manifest
(item versions and content hashes); ``import_archive()`` checks it strictly
and installs it as a pack (the previous version of that pack is kept).

Tools are written against generic inputs (time series, data frames, roles),
never against one house's signal ids, so they can move between houses.

**Audit** is the owner's flag, bound to the content hash of an item: any
change voids it, and identical content stays audited wherever it comes from
(re-import, the repository). **Test results** are kept per hash too. Both
live in ``<OBSERVER_HOME>/library/audits.yaml`` and ``tests.yaml``.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field

import yaml

KINDS = ("tools", "skills")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,60}$")
TYPES = ("series", "dataframe", "number", "integer", "string", "boolean", "list", "dict", "timestamp", "any")
ALLOWED_FILES = (".py", ".yaml", ".md")
MAX_ARCHIVE = 10 * 1024 * 1024
MAX_FILE = 1024 * 1024


@dataclass
class Source:
    id: str                    # house, path-<name>, pack-<name>: also the sandbox mount name
    kind: str                  # house | path | pack
    root: str
    name: str
    version: str = ""
    description: str = ""
    writable: bool = False
    repository: str = ""       # owner/name on GitHub, from library.yaml: where newer versions are released
    tag_prefix: str = "v"

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "version": self.version,
                "description": self.description, "root": self.root, "writable": self.writable,
                "repository": self.repository}


@dataclass
class Item:
    kind: str
    name: str
    source: Source
    folder: str
    meta: dict
    text: dict[str, str]       # file name -> content
    hash: str
    audit: dict | None = None
    test: dict | None = None
    shadows: list[str] = field(default_factory=list)   # sources with the same name, lower precedence

    @property
    def origin(self) -> str:
        return self.source.kind

    @property
    def version(self):
        return self.meta.get("version", 1)

    @property
    def audited(self) -> bool:
        return bool(self.audit and self.audit.get("audited"))

    @property
    def tests_passed(self) -> bool | None:
        if self.kind != "tools" or not self.test:
            return None
        return bool(self.test.get("passed"))

    def summary(self) -> dict:
        out = {"kind": self.kind, "name": self.name, "origin": self.origin, "source": self.source.id,
               "source_label": self.source.name + (f" {self.source.version}" if self.source.version else ""),
               "version": self.version, "audited": self.audited, "hash": self.hash[:12],
               "shadows": self.shadows, "created_by": self.meta.get("author") or self.meta.get("created_by")}
        if self.kind == "tools":
            out.update(purpose=self.meta.get("purpose", ""), needs=self.meta.get("needs", ""),
                       inputs=self.meta.get("inputs", []), outputs=self.meta.get("outputs", {}),
                       tests_passed=self.tests_passed)
        else:
            out.update(title=self.meta.get("title", self.name), description=self.meta.get("description", ""),
                       tools=self.meta.get("tools") or [])
        return out

    def detail(self) -> dict:
        return {**self.summary(), "meta": self.meta, "files": self.text, "audit": self.audit, "test": self.test}


def content_hash(text: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(text):
        h.update(name.encode() + b"\0" + text[name].encode() + b"\0")
    return h.hexdigest()


def front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        return yaml.safe_load(front) or {}, body.strip()
    return {}, text.strip()


def _read_item_files(folder: str) -> dict[str, str]:
    text = {}
    for f in sorted(os.listdir(folder)):
        p = os.path.join(folder, f)
        if os.path.isfile(p) and not f.startswith(".") and f.endswith(ALLOWED_FILES):
            text[f] = open(p, encoding="utf-8").read()
    return text


def _manifest(root: str, fallback: str) -> dict:
    path = os.path.join(root, "library.yaml")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}
    return {"name": fallback}


class Library:
    def __init__(self, house_dir: str, paths: list[str] | None = None):
        self.house_dir = house_dir
        self.paths = [os.path.abspath(os.path.expanduser(p)) for p in (paths or [])]
        self._lock = threading.Lock()
        for sub in ("tools", "skills", "packs", "history"):
            os.makedirs(os.path.join(house_dir, sub), exist_ok=True)

    # --- sources -------------------------------------------------------------------
    def sources(self) -> list[Source]:
        """Highest precedence first."""
        out = [Source("house", "house", self.house_dir, "house", writable=True,
                      description="items of this house: written by agents, the harvest or by hand")]
        for p in self.paths:
            m = _manifest(p, os.path.basename(p))
            name = str(m.get("name") or os.path.basename(p))
            out.append(Source("path-" + re.sub(r"[^a-z0-9_.-]", "_", name.lower()), "path", p, name,
                              str(m.get("version", "")), str(m.get("description", "")), False,
                              str(m.get("repository") or ""), str(m.get("tag_prefix") or "v")))
        packs = os.path.join(self.house_dir, "packs")
        for d in sorted(os.listdir(packs)):
            root = os.path.join(packs, d)
            if os.path.isdir(root) and not d.startswith("."):
                m = _manifest(root, d)
                out.append(Source("pack-" + d, "pack", root, str(m.get("name") or d), str(m.get("version", "")),
                                  str(m.get("description", "")), False, str(m.get("repository") or ""),
                                  str(m.get("tag_prefix") or "v")))
        return out

    def source(self, source_id: str) -> Source:
        for s in self.sources():
            if s.id == source_id:
                return s
        raise KeyError(f"no library source {source_id!r}")

    # --- state files ---------------------------------------------------------------
    def _state(self, name: str) -> dict:
        path = os.path.join(self.house_dir, name)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}

    def _save_state(self, name: str, data: dict) -> None:
        path = os.path.join(self.house_dir, name)
        with open(path + ".tmp", "w", encoding="utf-8") as fp:
            yaml.safe_dump(data, fp, sort_keys=True, allow_unicode=True, width=100)
        os.replace(path + ".tmp", path)

    # --- reading -----------------------------------------------------------------------
    def _load(self, kind: str, folder: str, source: Source, audits: dict, tests: dict) -> Item | None:
        text = _read_item_files(folder)
        if kind == "tools":
            if "tool.yaml" not in text:
                return None
            meta = yaml.safe_load(text["tool.yaml"]) or {}
        else:
            if "SKILL.md" not in text:
                return None
            meta, _ = front_matter(text["SKILL.md"])
        h = content_hash(text)
        return Item(kind, os.path.basename(folder), source, folder, meta, text, h, audits.get(h), tests.get(h))

    def items(self, kind: str) -> dict[str, Item]:
        if kind not in KINDS:
            raise ValueError(f"kind is one of {KINDS}")
        audits, tests = self._state("audits.yaml"), self._state("tests.yaml")
        out: dict[str, Item] = {}
        for source in self.sources():
            root = os.path.join(source.root, kind)
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                folder = os.path.join(root, name)
                if not (os.path.isdir(folder) and NAME.match(name)):
                    continue
                if name in out:
                    out[name].shadows.append(source.id)
                    continue
                item = self._load(kind, folder, source, audits, tests)
                if item:
                    out[name] = item
        return dict(sorted(out.items()))

    def get(self, kind: str, name: str) -> Item:
        item = self.items(kind).get(name)
        if item is None:
            raise KeyError(f"no {kind[:-1]} {name!r}")
        return item

    def tool_mounts(self) -> dict[str, str]:
        """{host tools folder: sandbox path} for every source that has tools."""
        return {os.path.join(s.root, "tools"): f"/opt/obs/tools/{s.id}" for s in self.sources()
                if os.path.isdir(os.path.join(s.root, "tools"))}

    def tool_index(self) -> dict:
        """What the sandbox needs to import tools: {name: {path, version, audited, ...}}."""
        return {n: {"origin": t.origin, "source": t.source.id, "path": f"/opt/obs/tools/{t.source.id}/{n}/tool.py",
                    "version": t.version, "audited": t.audited, "tests_passed": t.tests_passed,
                    "purpose": t.meta.get("purpose", "")} for n, t in self.items("tools").items()}

    # --- writing house items --------------------------------------------------------------
    def _keep_history(self, kind: str, name: str) -> dict | None:
        """Copy the current house version to history/; returns its meta."""
        folder = os.path.join(self.house_dir, kind, name)
        if not os.path.isdir(folder):
            return None
        audits, tests = self._state("audits.yaml"), self._state("tests.yaml")
        old = self._load(kind, folder, self.sources()[0], audits, tests)
        if old is None:
            return None
        target = os.path.join(self.house_dir, "history", kind, name, f"v{old.version}")
        if not os.path.exists(target):
            shutil.copytree(folder, target)
        return old.meta

    def history(self, kind: str, name: str) -> list[dict]:
        root = os.path.join(self.house_dir, "history", kind, name)
        if not os.path.isdir(root):
            return []
        out = []
        for v in sorted(os.listdir(root), key=lambda x: int(x[1:]) if x[1:].isdigit() else 0, reverse=True):
            text = _read_item_files(os.path.join(root, v))
            meta = (yaml.safe_load(text.get("tool.yaml", "")) or {}) if kind == "tools" \
                else front_matter(text.get("SKILL.md", ""))[0]
            out.append({"version": v, "hash": content_hash(text)[:12], "updated": meta.get("updated") or meta.get("created"),
                        "author": meta.get("author"), "files": text})
        return out

    def save_tool(self, name: str, purpose: str, inputs: list, outputs: dict, code: str, tests: str,
                  needs: str = "", author: str = "", requires: list | None = None) -> Item:
        if not NAME.match(name or ""):
            raise ValueError("name: lower case letters, digits and _, 2-41 characters, starting with a letter")
        if not purpose.strip():
            raise ValueError("purpose: say what the tool is for")
        clean_inputs = []
        for i in inputs or []:
            if not isinstance(i, dict) or not i.get("name"):
                raise ValueError("inputs: a list of {name, type, description, required, default}")
            t = i.get("type", "any")
            clean_inputs.append({"name": str(i["name"]), "type": t if t in TYPES else "any",
                                 "description": str(i.get("description", "")),
                                 "required": bool(i.get("required", True)),
                                 **({"default": i["default"]} if "default" in i else {})})
        if not isinstance(outputs, dict) or not outputs.get("description"):
            raise ValueError("outputs: {type, description} of what run() returns")
        if "def run(" not in code:
            raise ValueError("code: must define run(...), the tool's entry point")
        if "def test_" not in tests:
            raise ValueError("tests: pytest functions (def test_...) on synthetic data, importing `from tool import run`")
        now = time.strftime("%Y-%m-%d %H:%M")
        manifest = {"name": name, "version": 1, "purpose": purpose.strip(), "needs": needs.strip(),
                    "inputs": clean_inputs, "outputs": {"type": outputs.get("type", "any"),
                                                        "description": str(outputs["description"])},
                    "requires": requires or [], "author": author, "created": now}
        folder = os.path.join(self.house_dir, "tools", name)
        with self._lock:
            old = self._keep_history("tools", name)
            if old is None:
                other = self.items("tools").get(name)      # a library item of that name: continue its numbering
                if other is not None:
                    manifest["version"] = int(other.version) + 1
                    manifest["based_on"] = f"{other.source.id} v{other.version}"
            else:
                manifest.update(version=int(old.get("version", 1)) + 1, created=old.get("created", now), updated=now)
            os.makedirs(folder, exist_ok=True)
            for fname, content in (("tool.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True,
                                                                   width=100)),
                                   ("tool.py", code.rstrip() + "\n"), ("test_tool.py", tests.rstrip() + "\n")):
                with open(os.path.join(folder, fname), "w", encoding="utf-8") as fp:
                    fp.write(content)
        return self.get("tools", name)

    def save_skill(self, name: str, title: str, description: str, recipe: str, tools: list | None = None,
                   author: str = "") -> Item:
        if not NAME.match(name or ""):
            raise ValueError("name: lower case letters, digits and _, 2-41 characters, starting with a letter")
        if not recipe.strip() or not description.strip():
            raise ValueError("description and recipe are required")
        now = time.strftime("%Y-%m-%d %H:%M")
        front = {"title": title.strip() or name, "version": 1, "description": description.strip(),
                 "tools": tools or [], "author": author, "created": now}
        folder = os.path.join(self.house_dir, "skills", name)
        with self._lock:
            old = self._keep_history("skills", name)
            if old is None:
                other = self.items("skills").get(name)
                if other is not None:
                    front["version"] = int(other.version) + 1
                    front["based_on"] = f"{other.source.id} v{other.version}"
            else:
                front.update(version=int(old.get("version", 1)) + 1, created=old.get("created", now), updated=now)
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as fp:
                fp.write("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100) +
                         "---\n" + recipe.strip() + "\n")
        return self.get("skills", name)

    def delete(self, kind: str, name: str) -> None:
        item = self.get(kind, name)
        if item.origin != "house":
            raise PermissionError("only house items can be deleted; library folders and packs are managed as a whole")
        self._keep_history(kind, name)
        shutil.rmtree(item.folder)

    def set_audit(self, kind: str, name: str, audited: bool, by: str = "owner", note: str = "") -> Item:
        item = self.get(kind, name)
        with self._lock:
            audits = self._state("audits.yaml")
            if audited:
                audits[item.hash] = {"audited": True, "kind": kind, "name": name, "version": item.version,
                                     "by": by, "at": time.strftime("%Y-%m-%d %H:%M"), "note": note}
            else:
                audits.pop(item.hash, None)
            self._save_state("audits.yaml", audits)
        return self.get(kind, name)

    # --- tests --------------------------------------------------------------------------
    def run_tests(self, name: str, sandbox) -> dict:
        """Run the tool's tests in the sandbox on a copy of its folder; the result is kept per hash."""
        item = self.get("tools", name)
        with tempfile.TemporaryDirectory(prefix="obs-test-") as ws:
            target = os.path.join(ws, name)
            os.makedirs(target)
            for fname, content in item.text.items():
                with open(os.path.join(target, fname), "w", encoding="utf-8") as fp:
                    fp.write(content)
            code = ("import sys, pytest\n"
                    f"sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', '--no-header', '-rf', {name!r}]))\n")
            r = sandbox.run(code, ws, f"test_{name}")
        result = {"passed": r.exit_code == 0, "name": name, "version": item.version,
                  "at": time.strftime("%Y-%m-%d %H:%M"),
                  "output": (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()[-6000:],
                  "timed_out": r.timed_out}
        with self._lock:
            tests = self._state("tests.yaml")
            tests[item.hash] = result
            self._save_state("tests.yaml", tests)
        return result

    # --- export and import ---------------------------------------------------------------
    def export(self, items: list[tuple[str, str]] | None = None, name: str = "house-export",
               version: str = "", description: str = "") -> bytes:
        """A .tgz with library.yaml and the chosen items (default: all house items)."""
        if not PACK_NAME.match(name):
            raise ValueError("pack name: lower case letters, digits, . _ -")
        chosen: list[Item] = []
        if items is None:
            chosen = [i for kind in KINDS for i in self.items(kind).values() if i.origin == "house"]
        else:
            chosen = [self.get(kind, n) for kind, n in items]
        if not chosen:
            raise ValueError("nothing to export")
        manifest = {"name": name, "version": version or time.strftime("%Y.%m.%d-%H%M"),
                    "description": description, "exported": time.strftime("%Y-%m-%d %H:%M"),
                    "items": [{"kind": i.kind, "name": i.name, "version": i.version, "hash": i.hash,
                               "from": f"{i.source.id}"} for i in chosen]}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(path: str, data: bytes) -> None:
                info = tarfile.TarInfo(path)
                info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
                tar.addfile(info, io.BytesIO(data))
            add("library.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=100).encode())
            for i in chosen:
                for fname, content in i.text.items():
                    add(f"{i.kind}/{i.name}/{fname}", content.encode())
        return buf.getvalue()

    @staticmethod
    def read_archive(data: bytes) -> tuple[dict, dict[str, bytes]]:
        """Validated contents of an archive: (manifest, {relative path: bytes})."""
        if len(data) > MAX_ARCHIVE:
            raise ValueError(f"archive larger than {MAX_ARCHIVE // 1024 // 1024} MB")
        files: dict[str, bytes] = {}
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except tarfile.TarError as exc:
            raise ValueError(f"not a tar/tgz archive: {exc}") from exc
        with tar:
            for m in tar.getmembers():
                if m.isdir():
                    continue
                if not m.isfile():
                    raise ValueError(f"{m.name}: only regular files are allowed (no links or devices)")
                name = m.name[2:] if m.name.startswith("./") else m.name
                parts = name.split("/")
                if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts):
                    raise ValueError(f"{m.name}: unsafe path")
                if parts[0] in ("tools", "skills"):
                    if len(parts) != 3 or not NAME.match(parts[1]) or not parts[2].endswith(ALLOWED_FILES) \
                            or parts[2].startswith("."):
                        raise ValueError(f"{m.name}: expected tools|skills/<name>/<file>.py|.yaml|.md")
                elif parts not in (["library.yaml"], ["README.md"], ["CHANGELOG.md"]):
                    raise ValueError(f"{m.name}: unexpected file")
                if m.size > MAX_FILE:
                    raise ValueError(f"{m.name}: larger than {MAX_FILE // 1024} kB")
                files["/".join(parts)] = tar.extractfile(m).read()
        if "library.yaml" not in files:
            raise ValueError("library.yaml missing")
        manifest = yaml.safe_load(files["library.yaml"]) or {}
        if not PACK_NAME.match(str(manifest.get("name", ""))):
            raise ValueError("library.yaml: name missing or invalid")
        for path, content in files.items():
            if path.endswith(("tool.yaml", "SKILL.md")):
                content.decode("utf-8")
        return manifest, files

    def import_archive(self, data: bytes) -> dict:
        """Install an archive as a pack; a previous version of the same pack is kept in packs/.previous/."""
        manifest, files = self.read_archive(data)
        name = str(manifest["name"])
        root = os.path.join(self.house_dir, "packs", name)
        with self._lock:
            if os.path.isdir(root):
                old = _manifest(root, name)
                keep = os.path.join(self.house_dir, "packs", ".previous", name, str(old.get("version") or "unknown"))
                if os.path.exists(keep):
                    shutil.rmtree(keep)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.move(root, keep)
            for path, content in files.items():
                target = os.path.join(root, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fp:
                    fp.write(content)
        items = [p.split("/")[:2] for p in files if p.count("/") == 2]
        return {"pack": name, "version": manifest.get("version"), "items": sorted({"/".join(i) for i in items})}

    def import_url(self, url: str) -> dict:
        """Download an archive (e.g. a GitHub release asset) and install it as a pack."""
        from ..updates import download

        r = self.import_archive(download(url, MAX_ARCHIVE))
        r["url"] = url
        return r

    def remove_pack(self, name: str) -> None:
        root = os.path.join(self.house_dir, "packs", name)
        if not PACK_NAME.match(name) or not os.path.isdir(root):
            raise KeyError(f"no pack {name!r}")
        shutil.rmtree(root)
