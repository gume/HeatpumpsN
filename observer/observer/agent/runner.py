"""The agent loop and its audit record.

One run = one folder ``<OBSERVER_HOME>/runs/<id>/``:

* ``run.yaml``     — question, skill, model, limits, status, timing, usage, findings
* ``steps.jsonl``  — every step: the model's text, each tool call with its
                     arguments and full result
* ``report.md``    — the final report
* ``data/``, ``*.png``, ``.runs/*.py`` — what the tools and the sandboxed code wrote

The loop: the model gets the system prompt, the skill's recipe and the
question, calls tools until it calls ``finish`` (or a step or time limit
forces it to). It can read and compute; it cannot change anything.
"""
from __future__ import annotations

import json
import os
import threading
import time

import yaml

from .skills import Skill
from .tools import Toolbox

RESULT_LIMIT = 12_000

SYSTEM = """You are the analysis agent of a home heating observer. You investigate
recorded data of one house and report to its owner.

Rules:
* You only read and compute. You cannot change the plant, the database or the
  registry; changes you think are needed go into `proposals` for the owner.
* Ground every statement in data you fetched: name the signal ids, times (UTC)
  and values. Say clearly what you could not check.
* Start with `house_context`, then `find_signals` for exact ids. Never guess ids.
* Prefer `signal_features` and the summaries of `get_series` for quick looks;
  use `run_python` (sandbox, no network: work with the CSV files written by
  `get_series`, and the helpers in skill/ when a skill provides them) for
  anything that needs computation or plots.
* Be economical: fetch what the question needs, at a sensible resolution.
* End with `finish`: a concise Markdown report (question, what was checked,
  findings with evidence, what to do), findings with severity, confidence."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Run:
    """The record of one agent run on disk."""

    def __init__(self, folder: str):
        self.folder = folder
        self.id = os.path.basename(folder)

    def meta(self) -> dict:
        path = os.path.join(self.folder, "run.yaml")
        if not os.path.exists(path):
            return {"id": self.id}
        with open(path, encoding="utf-8") as fp:
            return {"id": self.id, **(yaml.safe_load(fp) or {})}

    def save_meta(self, meta: dict) -> None:
        tmp = os.path.join(self.folder, "run.yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            yaml.safe_dump({k: v for k, v in meta.items() if k != "id"}, fp, sort_keys=False,
                           allow_unicode=True, width=100)
        os.replace(tmp, os.path.join(self.folder, "run.yaml"))

    def add_step(self, step: dict) -> None:
        with open(os.path.join(self.folder, "steps.jsonl"), "a", encoding="utf-8") as fp:
            fp.write(json.dumps(step, ensure_ascii=False, default=str) + "\n")

    def steps(self) -> list[dict]:
        path = os.path.join(self.folder, "steps.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fp:
            return [json.loads(line) for line in fp if line.strip()]

    def report(self) -> str:
        path = os.path.join(self.folder, "report.md")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def user_prompt(question: str, skill: Skill | None, params: dict) -> str:
    parts = []
    if skill:
        parts.append(f"Skill: {skill.title}\n{skill.description}\n\nRecipe:\n{skill.recipe}")
        helpers = skill.helpers()
        if helpers:
            parts.append("Helper modules in the workspace: " + ", ".join(f"skill/{h}" for h in helpers) +
                         " (in run_python: import sys; sys.path.insert(0, 'skill'))")
    if params:
        parts.append("Parameters: " + json.dumps(params, ensure_ascii=False))
    parts.append(f"Current time (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    parts.append("Question from the owner: " + (question.strip() or "Carry out the recipe."))
    return "\n\n".join(parts)


def execute(run: Run, client, toolbox: Toolbox, question: str, skill: Skill | None = None,
            params: dict | None = None, max_steps: int = 25, deadline_s: float = 900,
            cancel: threading.Event | None = None) -> dict:
    """Run the agent loop to the end; everything is recorded in `run`."""
    params = params or {}
    meta = run.meta()
    meta.update(status="running", started=_now(), model=getattr(client, "model", ""),
                skill=skill.name if skill else None, question=question, params=params,
                limits={"max_steps": max_steps, "deadline_s": deadline_s}, usage={}, steps=0)
    run.save_meta(meta)
    installed = skill.install(toolbox.workspace) if skill else []
    names = skill.tools if skill and skill.tools else None
    if names and "finish" not in names:
        names = list(names) + ["finish"]
    tools = toolbox.schemas(names)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_prompt(question, skill, params)}]
    run.add_step({"n": 0, "time": _now(), "kind": "prompt", "text": messages[1]["content"], "helpers": installed})
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    started, nudged, status, error = time.monotonic(), False, "running", None
    try:
        for n in range(1, max_steps + 1):
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                break
            last = n == max_steps or time.monotonic() - started > deadline_s
            if last:
                messages.append({"role": "user", "content": "Limit reached: call finish now with what you have, "
                                 "and say what remained unchecked."})
            msg, use = client.chat_tools(messages, [t for t in tools if t["function"]["name"] == "finish"]
                                         if last else tools)
            for k in usage:
                usage[k] += int(use.get(k) or 0)
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            run.add_step({"n": n, "time": _now(), "kind": "model", "text": msg.get("content") or "",
                          "calls": [{"tool": c["function"]["name"], "args": c["function"].get("arguments")}
                                    for c in calls]})
            if not calls:
                if toolbox.finished is None and not nudged and not last:
                    nudged = True
                    messages.append({"role": "user", "content": "Continue with the tools, or call finish with "
                                     "the report."})
                    continue
                toolbox.finish(msg.get("content") or "(no report)", confidence=0.3)
                break
            for c in calls:
                name = c["function"]["name"]
                result_time = None
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    args, result = {}, {"error": f"arguments are not valid JSON: {exc}"}
                else:
                    t0 = time.monotonic()
                    result = toolbox.call(name, args)
                    result_time = round(time.monotonic() - t0, 2)
                text = json.dumps(result, ensure_ascii=False, default=str)
                run.add_step({"n": n, "time": _now(), "kind": "tool", "tool": name, "args": args,
                              "result": result, "seconds": result_time})
                if len(text) > RESULT_LIMIT:
                    text = text[:RESULT_LIMIT] + f'... (result cut at {RESULT_LIMIT} characters)'
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": name, "content": text})
            meta.update(steps=n, usage=usage)
            run.save_meta(meta)
            if toolbox.finished is not None:
                break
        if status == "running":
            status = "done" if toolbox.finished else "incomplete"
    except Exception as exc:          # recorded in the run; the service keeps going
        status, error = "error", f"{type(exc).__name__}: {exc}"
    fin = toolbox.finished or {}
    if fin.get("report_md"):
        with open(os.path.join(run.folder, "report.md"), "w", encoding="utf-8") as fp:
            fp.write(fin["report_md"].rstrip() + "\n")
    meta.update(status=status, error=error, finished=_now(), seconds=round(time.monotonic() - started, 1),
                usage=usage, findings=fin.get("findings") or [], confidence=fin.get("confidence"),
                proposals=fin.get("proposals") or [])
    run.save_meta(meta)
    return meta
