"""Starts agent runs in the background and serves their records to the portal.

One run at a time: runs are long (minutes) and the sandbox limits are per run.
Configuration (config.yaml, all optional):

    agent:
      read_token_env: OBSERVER_INFLUX_READ_TOKEN   # read-only InfluxDB token
      max_steps: 25
      deadline_s: 900
      sandbox: {runtime: bwrap, wall_s: 120, cpu_s: 120, memory_mb: 1024, tasks: 64, file_mb: 200}
"""
from __future__ import annotations

import os
import re
import threading
import time

from . import skills as skills_mod
from .data import DataSource
from .runner import Run, execute
from .sandbox import Limits, Sandbox
from .tools import Toolbox

_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]+$")


def make_sandbox(cfg: dict) -> Sandbox:
    a = cfg.get("agent") or {}
    sb = dict(a.get("sandbox") or {})
    runtime = sb.pop("runtime", "bwrap")
    image = sb.pop("image", "python:3.11-slim")
    limits = Limits(**{k: int(v) for k, v in sb.items() if k in Limits.__dataclass_fields__})
    return Sandbox(cfg["paths"]["sandbox_venv"], runtime, limits, image)


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
        self.sandbox = make_sandbox(cfg)
        self.data = make_data(cfg)
        a = cfg.get("agent") or {}
        self.max_steps, self.deadline_s = int(a.get("max_steps", 25)), float(a.get("deadline_s", 900))
        self._lock = threading.Lock()
        self._active: tuple[str, threading.Event] | None = None

    # --- views -------------------------------------------------------------------
    def skills(self) -> dict[str, skills_mod.Skill]:
        return skills_mod.load([self.cfg["paths"]["skills"]])

    def toolbox(self, workspace: str) -> Toolbox:
        return Toolbox(self.service, self.data, self.sandbox, workspace, self.cfg.get("context", ""))

    def status(self) -> dict:
        client = self.service.settings.client("agent")
        return {"sandbox": self.sandbox.status(), "database": "read-only token" if self.data else
                "missing: set OBSERVER_INFLUX_READ_TOKEN in .env", "model": getattr(client, "model", None),
                "ready": bool(client and self.data and self.sandbox.status()["ready"]),
                "active": self._active[0] if self._active else None,
                "limits": {"max_steps": self.max_steps, "deadline_s": self.deadline_s}}

    def tools(self) -> list[dict]:
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
                           | {"findings": len(m.get("findings") or [])})
        return out

    def _run(self, run_id: str) -> Run:
        if not _RUN_ID.match(run_id or "") or not os.path.isdir(os.path.join(self.runs_dir, run_id)):
            raise KeyError(run_id)
        return Run(os.path.join(self.runs_dir, run_id))

    def run(self, run_id: str) -> dict:
        r = self._run(run_id)
        files = self.toolbox(r.folder).list_files()["files"]
        return {"meta": r.meta(), "steps": r.steps(), "report": r.report(),
                "files": [f for f in files if f["path"] not in ("run.yaml", "steps.jsonl", "report.md")]}

    def file(self, run_id: str, path: str) -> str:
        r = self._run(run_id)
        full = os.path.abspath(os.path.join(r.folder, path))
        if not full.startswith(os.path.abspath(r.folder) + os.sep) or not os.path.isfile(full):
            raise KeyError(path)
        return full

    # --- starting and stopping ------------------------------------------------------------
    def start(self, skill: str | None, question: str, params: dict | None = None) -> str:
        client = self.service.settings.client("agent")
        if client is None:
            raise RuntimeError("no LLM configured for agent runs (Settings → task 'agent')")
        if self.data is None:
            raise RuntimeError("no read-only database token (OBSERVER_INFLUX_READ_TOKEN in .env)")
        chosen = None
        if skill:
            chosen = self.skills().get(skill)
            if chosen is None:
                raise KeyError(skill)
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
        params = {**(chosen.defaults if chosen else {}), **(params or {})}
        run = Run(folder)
        run.save_meta({"status": "starting", "skill": skill, "question": question})

        def work():
            try:
                execute(run, client, self.toolbox(folder), question, chosen, params,
                        min(chosen.max_steps, self.max_steps) if chosen else self.max_steps,
                        self.deadline_s, cancel)
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
