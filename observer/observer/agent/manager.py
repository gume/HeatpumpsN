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
        self._jobs: dict[str, dict] = {}

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
        name = name or ("own-tools-skills" if selection == "house" else "all-tools-skills")
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

    # Harvest proposals live in the run's harvest.yaml. Each proposal has a state
    # (pending, added, dismissed); the editors save drafts into it. A tool
    # proposal's code is "stale" once its description was edited: it must be
    # written again from the description before it can be accepted.
    TOOL_SPEC = ("name", "purpose", "needs", "inputs", "outputs", "tests_description")
    SKILL_FIELDS = ("name", "title", "description", "recipe", "tools")

    def _job(self, key: str, kind: str, model: str, work) -> dict:
        """Run `work()` in the background; the job (state, timing, error) is kept for the portal."""
        with self._lock:
            job = self._jobs.get(key)
            if job and job["state"] == "running":
                raise RuntimeError(f"{job['kind']} is still running (since {job['started']})")
            job = {"kind": kind, "state": "running", "model": model, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "t0": time.time()}
            self._jobs[key] = job

        def run():
            try:
                work()
                job["state"] = "done"
            except Exception as exc:            # shown in the portal
                job.update(state="error", error=f"{type(exc).__name__}: {exc}")
                if hasattr(exc, "info"):
                    job["llm"] = exc.info()         # the portal offers retry, wait or another model
            job["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            job["seconds"] = round(time.time() - job["t0"], 1)

        threading.Thread(target=run, name=f"job-{key}", daemon=True).start()
        return self.job(key)

    def job(self, key: str) -> dict | None:
        job = self._jobs.get(key)
        if job is None:
            return None
        out = {k: v for k, v in job.items() if k != "t0"}
        if job["state"] == "running":
            out["elapsed_s"] = round(time.time() - job["t0"])
        return out

    def _harvest_path(self, run_id: str) -> str:
        return os.path.join(self._run(run_id).folder, "harvest.yaml")

    def _save_harvest(self, run_id: str, h: dict) -> None:
        path = self._harvest_path(run_id)
        with open(path + ".tmp", "w", encoding="utf-8") as fp:
            yaml.safe_dump(h, fp, sort_keys=False, allow_unicode=True, width=100)
        os.replace(path + ".tmp", path)

    @staticmethod
    def _state(p: dict) -> str:
        return p.get("state") or ("added" if p.get("added") else "pending")

    def harvest(self, run_id: str, client=None) -> dict:
        """Ask the model for reusable tools and skills in a run (blocking; the portal uses start_harvest)."""
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
                p.update(state="pending", added=None)
                if kind == "tools":
                    p["code_stale"] = False
        self._save_harvest(run_id, out)
        return out

    def start_harvest(self, run_id: str) -> dict:
        self._run(run_id)
        client = self.service.settings.client("harvest")
        if client is None:
            raise RuntimeError("no LLM configured for the task 'harvest' (Settings)")
        return self._job(f"harvest:{run_id}", "harvest", client.model, lambda: self.harvest(run_id, client))

    def harvest_result(self, run_id: str) -> dict | None:
        path = self._harvest_path(run_id)
        h = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                h = yaml.safe_load(fp)
            for kind in ("tools", "skills"):
                for p in h.get(kind) or []:
                    p["state"] = self._state(p)
                    if kind == "tools" and not p.get("tests_description"):     # proposals from before 0.3.2
                        p["tests_description"] = [n.replace("_", " ")
                                                  for n in re.findall(r"def test_(\w+)", p.get("tests") or "")]
        job = self.job(f"harvest:{run_id}")
        if h is None and job is None:
            return None
        return {**(h or {"tools": [], "skills": []}), "job": job}

    def proposal(self, run_id: str, kind: str, index: int) -> tuple[dict, dict]:
        h = self.harvest_result(run_id)
        if not h or kind not in ("tools", "skills") or not 0 <= index < len(h.get(kind) or []):
            raise KeyError(f"{kind}[{index}]")
        h.pop("job", None)
        p = h[kind][index]
        p["job"] = self.job(f"code:{run_id}:{index}") if kind == "tools" else None
        return h, p

    def proposals(self) -> list[dict]:
        """Pending proposals of all runs (the Library tab's inbox)."""
        out = []
        for name in sorted(os.listdir(self.runs_dir), reverse=True):
            path = os.path.join(self.runs_dir, name, "harvest.yaml")
            if not (_RUN_ID.match(name) and os.path.exists(path)):
                continue
            with open(path, encoding="utf-8") as fp:
                h = yaml.safe_load(fp) or {}
            for kind in ("tools", "skills"):
                for i, p in enumerate(h.get(kind) or []):
                    if self._state(p) == "pending":
                        out.append({"run": name, "kind": kind, "index": i, "name": p.get("name"),
                                    "summary": p.get("purpose") if kind == "tools" else p.get("title") or p.get("description"),
                                    "edited": bool(p.get("edited")), "generated": h.get("generated"),
                                    "model": h.get("model")})
        return out

    def update_proposal(self, run_id: str, kind: str, index: int, fields: dict) -> dict:
        """Save an editor's draft. Editing a tool's description makes its code stale."""
        h, p = self.proposal(run_id, kind, index)
        p.pop("job", None)
        if self._state(p) != "pending":
            raise ValueError(f"this proposal is {self._state(p)}")
        allowed = self.TOOL_SPEC if kind == "tools" else self.SKILL_FIELDS
        changed = False
        for k in allowed:
            if k in fields and fields[k] != p.get(k):
                p[k] = fields[k]
                changed = True
        if changed:
            p["edited"] = True
            if kind == "tools":
                p.update(code_stale=True, test=None)
        self._save_harvest(run_id, h)
        return self.proposal(run_id, kind, index)[1]

    def start_write_tool(self, run_id: str, index: int) -> dict:
        """Let the model write code and tests from the (edited) description, then run the tests."""
        from ..agent.library import Library
        from ..llm import write_tool

        self.proposal(run_id, "tools", index)
        client = self.service.settings.client("harvest")
        if client is None:
            raise RuntimeError("no LLM configured for the task 'harvest' (Settings)")

        def work():
            h, p = self.proposal(run_id, "tools", index)
            p.pop("job", None)
            out = write_tool(client, p, p.get("code", ""), p.get("tests", ""))
            p.update(code=out["code"], tests=out["tests"], code_stale=False, written_by=client.model,
                     written=time.strftime("%Y-%m-%d %H:%M"))
            p["test"] = Library.test_files(p["name"], {"tool.py": p["code"], "test_tool.py": p["tests"]}, self.sandbox) \
                if self.sandbox.status()["ready"] else None
            self._save_harvest(run_id, h)

        return self._job(f"code:{run_id}:{index}", "write code", client.model, work)

    def test_proposal(self, run_id: str, index: int) -> dict:
        from ..agent.library import Library

        h, p = self.proposal(run_id, "tools", index)
        p.pop("job", None)
        if p.get("code_stale"):
            raise ValueError("the code does not match the edited description: write it again first")
        if not self.sandbox.status()["ready"]:
            raise RuntimeError("sandbox not ready: " + "; ".join(self.sandbox.status()["problems"]))
        p["test"] = Library.test_files(p["name"], {"tool.py": p["code"], "test_tool.py": p["tests"]}, self.sandbox)
        self._save_harvest(run_id, h)
        return p["test"]

    def dismiss_proposal(self, run_id: str, kind: str, index: int) -> None:
        h, p = self.proposal(run_id, kind, index)
        p.pop("job", None)
        p["state"] = "dismissed"
        self._save_harvest(run_id, h)

    def accept_harvest(self, run_id: str, kind: str, index: int) -> dict:
        """Add one proposal (with the editor's changes) to the house library, unaudited."""
        h, p = self.proposal(run_id, kind, index)
        p.pop("job", None)
        if self._state(p) != "pending":
            raise ValueError(f"this proposal is {self._state(p)}")
        author = f"harvest of run {run_id}"
        if kind == "tools":
            if p.get("code_stale"):
                raise ValueError("the description was edited: write the code from it first")
            self.library.save_tool(p["name"], p["purpose"], p.get("inputs") or [], p["outputs"], p["code"],
                                   p["tests"], p.get("needs", ""), author=author,
                                   tests_description=p.get("tests_description") or [])
            test = self.library.run_tests(p["name"], self.sandbox) if self.sandbox.status()["ready"] \
                else {"passed": None, "output": "sandbox not ready: tests not run"}
            result = {"name": p["name"], "tests_passed": test["passed"], "test_output": test["output"][-3000:]}
        else:
            tools = p.get("tools") or []
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            self.library.save_skill(p["name"], p.get("title", ""), p["description"], p["recipe"], tools,
                                    author=author)
            result = {"name": p["name"]}
        p.update(state="added", added=time.strftime("%Y-%m-%d %H:%M"))
        self._save_harvest(run_id, h)
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
