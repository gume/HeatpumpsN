"""The library of analysis tools and skills, with tests and audit flags.

Two kinds of items, each in two places:

* **tools**: code that runs in the sandbox. A folder with ``tool.yaml``
  (name, purpose, inputs, outputs, what data it needs), ``tool.py`` (a
  ``run(...)`` function) and ``test_tool.py`` (pytest, synthetic data only);
* **skills**: recipes, a folder with ``SKILL.md`` (front matter + Markdown).

``package`` items ship with the observer (``observer/library/``, in git);
``house`` items live in ``<OBSERVER_HOME>/library/`` and are written by
agents (or by hand). A house item overrides a package item of the same name.

Tools are written against generic inputs (time series, data frames, roles),
never against one house's signal ids, so they can move to another house with
other sensors or no controller at all.

**Audit** is a flag the owner sets per item. It is bound to a hash of the
item's files: any change voids it. **Test results** are kept per hash too.
Both live in ``<OBSERVER_HOME>/library/audits.yaml`` and ``tests.yaml``, so
package folders are never written.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field

import yaml

PACKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "library")
KINDS = ("tools", "skills")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TYPES = ("series", "dataframe", "number", "integer", "string", "boolean", "list", "dict", "timestamp", "any")


@dataclass
class Item:
    kind: str
    name: str
    origin: str               # package | house
    folder: str
    meta: dict
    text: dict[str, str]      # file name -> content
    hash: str
    audit: dict | None = None
    test: dict | None = None
    overrides: bool = False

    @property
    def audited(self) -> bool:
        return bool(self.audit and self.audit.get("hash") == self.hash and self.audit.get("audited"))

    @property
    def tests_passed(self) -> bool | None:
        if self.kind != "tools":
            return None
        if not self.test or self.test.get("hash") != self.hash:
            return None
        return bool(self.test.get("passed"))

    def summary(self) -> dict:
        out = {"kind": self.kind, "name": self.name, "origin": self.origin, "audited": self.audited,
               "hash": self.hash[:12], "overrides_package": self.overrides,
               "created_by": self.meta.get("author") or self.meta.get("created_by")}
        if self.kind == "tools":
            out.update(purpose=self.meta.get("purpose", ""), needs=self.meta.get("needs", ""),
                       inputs=self.meta.get("inputs", []), outputs=self.meta.get("outputs", {}),
                       tests_passed=self.tests_passed)
        else:
            out.update(title=self.meta.get("title", self.name), description=self.meta.get("description", ""),
                       tools=self.meta.get("tools") or [])
        return out

    def detail(self) -> dict:
        return {**self.summary(), "meta": self.meta, "files": self.text,
                "audit": self.audit if self.audited else None,
                "test": self.test if self.test and self.test.get("hash") == self.hash else None}


def _hash(text: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(text):
        h.update(name.encode() + b"\0" + text[name].encode() + b"\0")
    return h.hexdigest()


def _front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        return yaml.safe_load(front) or {}, body.strip()
    return {}, text.strip()


class Library:
    def __init__(self, house_dir: str, package_dir: str = PACKAGE_DIR):
        self.package_dir, self.house_dir = package_dir, house_dir
        self._lock = threading.Lock()
        for kind in KINDS:
            os.makedirs(os.path.join(house_dir, kind), exist_ok=True)

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
    def _load(self, kind: str, folder: str, origin: str) -> Item | None:
        text = {}
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and not f.startswith(".") and f.endswith((".py", ".yaml", ".md")):
                text[f] = open(p, encoding="utf-8").read()
        if kind == "tools":
            if "tool.yaml" not in text:
                return None
            meta = yaml.safe_load(text["tool.yaml"]) or {}
        else:
            if "SKILL.md" not in text:
                return None
            meta, _ = _front_matter(text["SKILL.md"])
        name = os.path.basename(folder)
        key = f"{kind}/{origin}/{name}"
        return Item(kind, name, origin, folder, meta, text, _hash(text),
                    self._state("audits.yaml").get(key), self._state("tests.yaml").get(key))

    def items(self, kind: str) -> dict[str, Item]:
        if kind not in KINDS:
            raise ValueError(f"kind is one of {KINDS}")
        out: dict[str, Item] = {}
        for root, origin in ((os.path.join(self.package_dir, kind), "package"),
                             (os.path.join(self.house_dir, kind), "house")):
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                folder = os.path.join(root, name)
                if os.path.isdir(folder) and NAME.match(name):
                    item = self._load(kind, folder, origin)
                    if item:
                        item.overrides = name in out
                        out[name] = item
        return out

    def get(self, kind: str, name: str) -> Item:
        item = self.items(kind).get(name)
        if item is None:
            raise KeyError(f"no {kind[:-1]} {name!r}")
        return item

    def tool_index(self) -> dict:
        """What the sandbox needs to import tools: {name: {path, audited, origin}}."""
        return {n: {"origin": t.origin, "audited": t.audited, "tests_passed": t.tests_passed,
                    "purpose": t.meta.get("purpose", "")} for n, t in self.items("tools").items()}

    # --- writing (house items only) ------------------------------------------------------
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
        manifest = {"name": name, "version": 1, "purpose": purpose.strip(), "needs": needs.strip(),
                    "inputs": clean_inputs, "outputs": {"type": outputs.get("type", "any"),
                                                        "description": str(outputs["description"])},
                    "requires": requires or [], "author": author,
                    "created": time.strftime("%Y-%m-%d %H:%M")}
        folder = os.path.join(self.house_dir, "tools", name)
        with self._lock:
            old = self.items("tools").get(name)
            if old is not None and old.origin == "house":
                manifest["version"] = int(old.meta.get("version", 1)) + 1
                manifest["created"] = old.meta.get("created", manifest["created"])
                manifest["updated"] = time.strftime("%Y-%m-%d %H:%M")
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
        front = {"title": title.strip() or name, "description": description.strip(), "tools": tools or [],
                 "author": author, "created": time.strftime("%Y-%m-%d %H:%M")}
        folder = os.path.join(self.house_dir, "skills", name)
        with self._lock:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as fp:
                fp.write("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100) +
                         "---\n" + recipe.strip() + "\n")
        return self.get("skills", name)

    def delete(self, kind: str, name: str) -> None:
        item = self.get(kind, name)
        if item.origin != "house":
            raise PermissionError("package items are part of the observer's code; only house items can be deleted")
        shutil.rmtree(item.folder)

    def set_audit(self, kind: str, name: str, audited: bool, by: str = "owner", note: str = "") -> Item:
        item = self.get(kind, name)
        key = f"{kind}/{item.origin}/{name}"
        with self._lock:
            audits = self._state("audits.yaml")
            if audited:
                audits[key] = {"audited": True, "hash": item.hash, "by": by, "at": time.strftime("%Y-%m-%d %H:%M"),
                               "note": note}
            else:
                audits.pop(key, None)
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
        result = {"passed": r.exit_code == 0, "hash": item.hash, "at": time.strftime("%Y-%m-%d %H:%M"),
                  "output": (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()[-6000:],
                  "timed_out": r.timed_out}
        with self._lock:
            tests = self._state("tests.yaml")
            tests[f"tools/{item.origin}/{name}"] = result
            self._save_state("tests.yaml", tests)
        return result
