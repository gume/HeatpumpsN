"""Starts agent runs in the background and serves runs and the library to the portal.

One run at a time: runs are long (minutes) and the sandbox limits are per run.
Each run gets its own data socket (``broker.py``) for the code in the sandbox.
Configuration (config.yaml, all optional):

    agent:
      read_token_env: OBSERVER_INFLUX_READ_TOKEN   # read-only InfluxDB token
      max_steps: 25
      deadline_s: 1800
      sandbox: {runtime: bwrap, wall_s: 120, cpu_s: 120, memory_mb: 1024, tasks: 64, file_mb: 200}
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import yaml

from .broker import DataBroker
from .data import DataSource
from .library import Library
from .runner import Run, execute
from .sandbox import Limits, Sandbox
from .tools import Toolbox

_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9_-]+$")


def make_library(cfg: dict) -> Library:
    """House items in the data directory plus the library folders named in agent.library_paths."""
    return Library(cfg["paths"]["library"], (cfg.get("agent") or {}).get("library_paths") or [])


def make_sandbox(cfg: dict, library: Library | None = None) -> Sandbox:
    a = cfg.get("agent") or {}
    sb = dict(a.get("sandbox") or {})
    runtime = sb.pop("runtime", "bwrap")
    image = sb.pop("image", "python:3.11-slim")
    limits = Limits(**{k: int(v) for k, v in sb.items() if k in Limits.__dataclass_fields__})
    return Sandbox(cfg["paths"]["sandbox_venv"], runtime, limits, image,
                   library.tool_mounts if library is not None else None)


def make_data(cfg: dict) -> DataSource | None:
    i, a = cfg.get("influx") or {}, cfg.get("agent") or {}
    token = os.environ.get(a.get("read_token_env", "OBSERVER_INFLUX_READ_TOKEN"), "")
    if not token or not i.get("url"):
        return None
    return DataSource(i["url"], i.get("org", ""), i.get("bucket", ""), token)


class AgentManager:
    def __init__(self, service, cfg: dict):
        self.service, self.cfg = service, cfg
        self.runs_dir = cfg["paths"]["runs"]
        os.makedirs(self.runs_dir, exist_ok=True)
        self.library = make_library(cfg)
        self.sandbox = make_sandbox(cfg, self.library)
        self.data = make_data(cfg)
        a = cfg.get("agent") or {}
        self.max_steps, self.deadline_s = int(a.get("max_steps", 25)), float(a.get("deadline_s", 1800))
        self._lock = threading.Lock()
        self._active: tuple[str, threading.Event] | None = None

    # --- views -------------------------------------------------------------------
    def toolbox(self, workspace: str, author: str = "") -> Toolbox:
        return Toolbox(self.service, self.data, self.sandbox, workspace, self.cfg.get("context", ""),
                       self.library, author)

    def status(self) -> dict:
        client = self.service.settings.client("agent")
        return {"sandbox": self.sandbox.status(), "database": "read-only token" if self.data else
                "missing: set OBSERVER_INFLUX_READ_TOKEN in .env", "model": getattr(client, "model", None),
                "ready": bool(client and self.data and self.sandbox.status()["ready"]),
                "active": self._active[0] if self._active else None,
                "limits": {"max_steps": self.max_steps, "deadline_s": self.deadline_s}}

    def core_tools(self) -> list[dict]:
        tb = Toolbox(self.service, None, None, os.path.join(self.runs_dir, ".probe"))
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tb.tools.values()]

    def runs(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.runs_dir), reverse=True):
            if _RUN_ID.match(name) and os.path.exists(os.path.join(self.runs_dir, name, "run.yaml")):
                m = Run(os.path.join(self.runs_dir, name)).meta()
                out.append({k: m.get(k) for k in ("id", "status", "skill", "question", "model", "started",
                                                  "seconds", "steps", "confidence")}
                           | {"findings": len(m.get("findings") or []),
                              "created": len(m.get("library_created") or [])})
        return out

    def _run(self, run_id: str) -> Run:
        if not _RUN_ID.match(run_id or "") or not os.path.isdir(os.path.join(self.runs_dir, run_id)):
            raise KeyError(run_id)
        return Run(os.path.join(self.runs_dir, run_id))

    def run(self, run_id: str) -> dict:
        r = self._run(run_id)
        files = self.toolbox(r.folder).list_files()["files"]
        return {"meta": r.meta(), "steps": r.steps(), "report": r.report(), "harvest": self.harvest_result(run_id),
                "files": [f for f in files if f["path"] not in ("run.yaml", "steps.jsonl", "report.md",
                                                                "harvest.yaml", "state.json")]}

    def file(self, run_id: str, path: str) -> str:
        r = self._run(run_id)
        full = os.path.abspath(os.path.join(r.folder, path))
        if not full.startswith(os.path.abspath(r.folder) + os.sep) or not os.path.isfile(full):
            raise KeyError(path)
        return full

    # --- the library ---------------------------------------------------------------------
    def library_view(self) -> dict:
        return {"sources": [s.view() for s in self.library.sources()],
                **{kind: [i.summary() for i in self.library.items(kind).values()] for kind in ("tools", "skills")}}

    def library_item(self, kind: str, name: str) -> dict:
        item = self.library.get(kind, name)
        return {**item.detail(), "history": [{k: v for k, v in h.items() if k != "files"}
                                             for h in self.library.history(kind, name)]}

    def export_library(self, selection: str = "house", items: list | None = None, name: str = "",
                       version: str = "") -> tuple[str, bytes]:
        """(file name, .tgz): selection house | all | items ([[kind, name], ...])."""
        if selection == "all":
            chosen = [(k, n) for k in ("tools", "skills") for n in self.library.items(k)]
        elif selection == "items":
            chosen = [(str(k), str(n)) for k, n in items or []]
        else:
            chosen = None
        name = name or ("house-export" if selection == "house" else "library-export")
        version = version or time.strftime("%Y.%m.%d-%H%M")
        return f"{name}-{version}.tgz", self.library.export(chosen, name, version)

    def import_library(self, data: bytes) -> dict:
        return self.library.import_archive(data)

    def import_library_url(self, url: str) -> dict:
        return self.library.import_url(url)

    def remove_pack(self, name: str) -> None:
        self.library.remove_pack(name)

    def test_tool(self, name: str) -> dict:
        return self.library.run_tests(name, self.sandbox)

    def audit(self, kind: str, name: str, audited: bool, note: str = "") -> dict:
        return self.library.set_audit(kind, name, audited, "owner", note).summary()

    def delete_item(self, kind: str, name: str) -> None:
        self.library.delete(kind, name)

    # --- harvest: reusable tools and skills from a finished run -------------------------
    def _digest(self, run: Run, limit: int = 60_000) -> dict:
        meta = run.meta()
        steps = []
        for s in run.steps():
            if s["kind"] == "model" and s.get("text"):
                steps.append({"model": s["text"][:1500]})
            elif s["kind"] == "tool":
                r = s.get("result") if isinstance(s.get("result"), dict) else {}
                if s["tool"] == "run_python":
                    steps.append({"run_python": s["args"].get("code", ""), "exit_code": r.get("exit_code"),
                                  "stdout": (r.get("stdout") or "")[-1500:], "stderr": (r.get("stderr") or "")[-800:]})
                elif s["tool"] != "finish":
                    steps.append({"tool": s["tool"], "args": s.get("args"), "error": r.get("error")})
        digest = {"question": meta.get("question"), "skill": meta.get("skill"), "status": meta.get("status"),
                  "report": run.report()[:12_000], "library_used": meta.get("library_used"),
                  "library_created": meta.get("library_created"), "steps": steps}
        while len(json.dumps(digest, default=str)) > limit and len(digest["steps"]) > 4:
            digest["steps"] = digest["steps"][:2] + digest["steps"][3:]     # drop from the middle, keep the ends
        return digest

    def harvest(self, run_id: str, client=None) -> dict:
        from ..llm import harvest_run

        run = self._run(run_id)
        client = client or self.service.settings.client("harvest")
        if client is None:
            raise RuntimeError("no LLM configured for the task 'harvest' (Settings)")
        lib = {kind: [{k: v for k, v in i.summary().items() if k in ("name", "purpose", "title", "description",
                                                                       "needs", "audited")}
                      for i in self.library.items(kind).values()] for kind in ("tools", "skills")}
        out = harvest_run(client, self._digest(run), lib)
        out.update(generated=time.strftime("%Y-%m-%d %H:%M"), model=getattr(client, "model", ""))
        for kind in ("tools", "skills"):
            for p in out[kind]:
                p["added"] = None
        with open(os.path.join(run.folder, "harvest.yaml"), "w", encoding="utf-8") as fp:
            yaml.safe_dump(out, fp, sort_keys=False, allow_unicode=True, width=100)
        return out

    def harvest_result(self, run_id: str) -> dict | None:
        path = os.path.join(self._run(run_id).folder, "harvest.yaml")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fp:
            return yaml.safe_load(fp)

    def accept_harvest(self, run_id: str, kind: str, index: int) -> dict:
        """Add one proposal to the house library (unaudited); a tool's tests run at once."""
        run = self._run(run_id)
        h = self.harvest_result(run_id)
        if not h or kind not in ("tools", "skills") or not 0 <= index < len(h[kind]):
            raise KeyError(f"{kind}[{index}]")
        p = h[kind][index]
        author = f"harvest of run {run.id}"
        if kind == "tools":
            self.library.save_tool(p["name"], p["purpose"], p.get("inputs") or [], p["outputs"], p["code"],
                                   p["tests"], p.get("needs", ""), author=author)
            test = self.library.run_tests(p["name"], self.sandbox)
            result = {"name": p["name"], "tests_passed": test["passed"], "test_output": test["output"][-3000:]}
        else:
            self.library.save_skill(p["name"], p.get("title", ""), p["description"], p["recipe"], p.get("tools"),
                                    author=author)
            result = {"name": p["name"]}
        p["added"] = time.strftime("%Y-%m-%d %H:%M")
        with open(os.path.join(run.folder, "harvest.yaml"), "w", encoding="utf-8") as fp:
            yaml.safe_dump(h, fp, sort_keys=False, allow_unicode=True, width=100)
        return result

    # --- starting and stopping ------------------------------------------------------------
    def execute_in(self, folder: str, client, question: str, skill=None, cancel: threading.Event | None = None,
                   max_steps: int | None = None, resume: bool = False) -> dict:
        """One run in `folder`, with its data socket open for the sandboxed code."""
        run = Run(folder)
        toolbox = self.toolbox(folder, author=f"agent run {os.path.basename(folder)}")
        with DataBroker(toolbox, self.library, os.path.join(folder, "queries.jsonl")) as broker:
            toolbox.broker = broker
            return execute(run, client, toolbox, question, skill, {}, max_steps or self.max_steps,
                           self.deadline_s, cancel, resume)

    def resume(self, run_id: str) -> str:
        """Continue a paused run from its last saved step, with the model now set for the task 'agent'."""
        run = self._run(run_id)
        meta = run.meta()
        if meta.get("status") != "paused" or not os.path.exists(os.path.join(run.folder, "state.json")):
            raise ValueError("only a paused run with a saved state can be resumed")
        client = self.service.settings.client("agent")
        if client is None:
            raise RuntimeError("no LLM configured for agent runs (Settings → task 'agent')")
        with self._lock:
            if self._active:
                raise RuntimeError(f"run {self._active[0]} is still running")
            cancel = threading.Event()
            self._active = (run_id, cancel)

        def work():
            try:
                self.execute_in(run.folder, client, meta.get("question") or "", None, cancel,
                                int((meta.get("limits") or {}).get("max_steps") or self.max_steps), resume=True)
            finally:
                with self._lock:
                    self._active = None

        threading.Thread(target=work, name=f"agent-{run_id}-resume", daemon=True).start()
        return run_id

    def start(self, skill: str | None, question: str) -> str:
        client = self.service.settings.client("agent")
        if client is None:
            raise RuntimeError("no LLM configured for agent runs (Settings → task 'agent')")
        if self.data is None:
            raise RuntimeError("no read-only database token (OBSERVER_INFLUX_READ_TOKEN in .env)")
        chosen = self.library.get("skills", skill) if skill else None
        if not chosen and not question.strip():
            raise ValueError("give a question or choose a skill")
        with self._lock:
            if self._active:
                raise RuntimeError(f"run {self._active[0]} is still running")
            run_id = time.strftime("%Y%m%d-%H%M%S-") + (skill or "question")
            folder = os.path.join(self.runs_dir, run_id)
            os.makedirs(folder)
            cancel = threading.Event()
            self._active = (run_id, cancel)
        Run(folder).save_meta({"status": "starting", "skill": skill, "question": question})

        def work():
            try:
                self.execute_in(folder, client, question, chosen, cancel)
            finally:
                with self._lock:
                    self._active = None

        threading.Thread(target=work, name=f"agent-{run_id}", daemon=True).start()
        return run_id

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            if self._active and self._active[0] == run_id:
                self._active[1].set()
                return True
        return False
