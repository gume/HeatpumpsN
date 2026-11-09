"""The agent loop and its audit record.

One run = one folder ``<OBSERVER_HOME>/runs/<id>/``:

* ``run.yaml``     — question, skill, model, limits, status, timing, usage, findings
* ``steps.jsonl``  — every step: the model's text, each tool call with its
                     arguments and full result
* ``report.md``    — the final report
* ``queries.jsonl`` — every request the sandboxed code made to the data socket
* ``data/``, ``*.png``, ``.runs/*.py`` — what the tools and the sandboxed code wrote

The loop: the model gets the system prompt, the chosen skill's recipe (if
any) and the question, and calls tools until it calls ``finish`` (or a step
or time limit forces it to). It decides itself which data and time windows
it needs; its code reads the data through the run's data socket. It can read,
compute and add unaudited tools and skills to the library; it cannot change
the plant, the database or the registry. ``run.yaml`` records which library
items were used and whether they were audited.
"""
from __future__ import annotations

import json
import os
import threading
import time

import yaml

from .tools import Toolbox

RESULT_LIMIT = 12_000

SYSTEM = """You are the analysis agent of a home heating observer. You investigate
recorded data of one house and report to its owner.

Rules:
* You only read and compute. You cannot change the plant, the database or the
  registry; changes you think are needed go into `proposals` for the owner.
* Start with `house_context` (the house description and the owner's description
  in it are authoritative), then `find_signals` for exact ids. Never guess ids.
* You choose the data and the time windows: `data_coverage` shows how much
  history exists. Quick looks: `signal_features`, `get_series` summaries,
  `get_events`. Real analysis: `run_python`, where your code reads the data
  itself (`obs.data.series(...)`) and can use the library's tools
  (`obs.tools.load(name).run(...)`). What-if questions about the mixer
  control (other parameters, setpoints, radiator states, a recorded
  morning replayed): `obs.sim`, the controller's own MPC core in simulation
  (`help(obs.sim)` shows its use; a skill may describe the method).
* The library: `list_library` (tools and skills, with audit flags). Check for
  a skill that fits the question and follow it; prefer audited tools. When a
  result depends on an unaudited tool or skill, say so in the report.
* When you write analysis code that would be useful again, make it a tool
  with `create_tool`: universal (generic series/frames, no signal ids of this
  house), with purpose, inputs, output and tests on synthetic data. When a
  method worked well, `save_skill` it. Both stay unaudited until the owner
  checks them.
* Ground every statement in data you fetched: name the signal ids, times (UTC)
  and values. Say clearly what you could not check.
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


def skill_text(skill) -> str:
    """The recipe of a library skill item, with its audit state."""
    body = skill.text["SKILL.md"].split("---", 2)[-1].strip() if skill.text["SKILL.md"].startswith("---") \
        else skill.text["SKILL.md"]
    state = "audited by the owner" if skill.audited else "NOT audited by the owner"
    tools = skill.meta.get("tools") or []
    return (f"Skill chosen by the owner: {skill.meta.get('title', skill.name)} ({skill.name}, {state})\n"
            f"{skill.meta.get('description', '')}\n" + (f"Library tools it uses: {', '.join(tools)}\n" if tools else "") +
            f"\nRecipe:\n{body}")


def library_index(library) -> str:
    """A short index of the library for the prompt, so the agent sees what exists without a call."""
    if library is None:
        return ""
    flag = lambda i: "audited" if i.audited else "not audited"
    skills = [f"- {i.name}: {i.meta.get('description', '')} ({flag(i)})" for i in library.items("skills").values()]
    tools = [f"- {i.name}: {(i.meta.get('purpose') or '').split('. ')[0]} ({flag(i)})"
             for i in library.items("tools").values()]
    return ("Library (read an item with read_library_item before using it):\nSkills:\n" + "\n".join(skills or ["- none"]) +
            "\nTools (in run_python: obs.tools.load(name).run(...)):\n" + "\n".join(tools or ["- none"]))


def user_prompt(question: str, skill, params: dict, library=None) -> str:
    parts = []
    if skill:
        parts.append(skill_text(skill))
    elif library is not None:
        parts.append("No skill was chosen: if one of the library's skills fits the question, read it and follow it.")
    index = library_index(library)
    if index:
        parts.append(index)
    if params:
        parts.append("Parameters: " + json.dumps(params, ensure_ascii=False))
    parts.append(f"Current time (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    parts.append("Question from the owner: " + (question.strip() or "Carry out the recipe."))
    return "\n\n".join(parts)


def pause_reason(exc: Exception) -> str | None:
    """Why the LLM is unavailable, when the run should wait instead of failing (None: a real error)."""
    import httpx

    from ..llm import LlmUnavailable

    if isinstance(exc, LlmUnavailable):
        return f"LLM not available ({exc.kind}): {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or code >= 500:
            from ..llm import human_duration, retry_after_s

            delay = retry_after_s(exc.response) if code == 429 else None
            when = f"; the provider says retry in {human_duration(delay)}" if delay is not None else ""
            text = exc.response.text
            try:
                body = json.loads(text)
                body = body[0] if isinstance(body, list) and body else body
                text = (body.get("error") or {}).get("message") or text
            except (ValueError, AttributeError):
                pass
            kind = "quota or rate limit" if code == 429 else "provider error"
            return f"LLM {kind} (HTTP {code}){when}. {text[:300].strip()}"
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return f"LLM endpoint not reachable: {type(exc).__name__}: {exc}"
    return None


def _strip_signatures(messages: list[dict]) -> list[dict]:
    """Provider-specific signatures (e.g. Gemini's on tool calls) are not valid for another model."""
    out = []
    for m in messages:
        if m.get("tool_calls"):
            m = {**m, "tool_calls": [{k: v for k, v in c.items() if k != "extra_content"} for c in m["tool_calls"]]}
        out.append({k: v for k, v in m.items() if k != "extra_content"})
    return out


def execute(run: Run, client, toolbox: Toolbox, question: str, skill=None,
            params: dict | None = None, max_steps: int = 25, deadline_s: float = 900,
            cancel: threading.Event | None = None, resume: bool = False) -> dict:
    """Run the agent loop to the end (or until it pauses); everything is recorded in `run`.

    After every step the conversation is saved in state.json. When the LLM is
    unavailable (quota, rate limit, provider down) the run is "paused" instead
    of failed; execute(..., resume=True) continues it later, possibly with
    another model."""
    params = params or {}
    meta = run.meta()
    model = getattr(client, "model", "")
    state_path = os.path.join(run.folder, "state.json")
    if resume:
        with open(state_path, encoding="utf-8") as fp:
            state = json.load(fp)
        messages, first, usage, nudged = state["messages"], state["n"] + 1, state["usage"], state.get("nudged", False)
        toolbox.skills_read.update(state.get("skills_read") or {})
        toolbox.created.extend(state.get("created") or [])
        tools_before = dict((meta.get("library_used") or {}).get("tools") or {})
        requests_before = int(meta.get("data_requests") or 0)
        if model not in (meta.get("models") or [meta.get("model")]):
            messages = _strip_signatures(messages)
        meta.pop("llm", None)
        meta.update(status="running", resumed=_now(), model=model, error=None,
                    models=list(dict.fromkeys((meta.get("models") or [meta.get("model")]) + [model])))
        run.add_step({"n": first, "time": _now(), "kind": "resume", "text": f"resumed with {model}"})
    else:
        meta.update(status="running", started=_now(), model=model, models=[model],
                    skill=skill.name if skill else None, question=question, params=params,
                    limits={"max_steps": max_steps, "deadline_s": deadline_s}, usage={}, steps=0)
        if skill:
            toolbox.skills_read[skill.name] = skill.audited
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user_prompt(question, skill, params, toolbox.library)}]
        run.add_step({"n": 0, "time": _now(), "kind": "prompt", "text": messages[1]["content"]})
        first, usage, nudged, tools_before, requests_before = 1, {"prompt_tokens": 0, "completion_tokens": 0}, \
            False, {}, 0
    run.save_meta(meta)
    tools = toolbox.schemas()

    def save_state(n: int) -> None:
        with open(state_path + ".tmp", "w", encoding="utf-8") as fp:
            json.dump({"messages": messages, "n": n, "usage": usage, "nudged": nudged,
                       "skills_read": toolbox.skills_read, "created": toolbox.created}, fp, ensure_ascii=False,
                      default=str)
        os.replace(state_path + ".tmp", state_path)

    started, status, error = time.monotonic(), "running", None
    try:
        for n in range(first, max_steps + 1):
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                break
            last = n == max_steps or time.monotonic() - started > deadline_s
            ask = messages + ([{"role": "user", "content": "Limit reached: call finish now with what you have, "
                                "and say what remained unchecked."}] if last else [])
            msg, use = client.chat_tools(ask, [t for t in tools if t["function"]["name"] == "finish"]
                                         if last else tools)
            messages = ask
            for k in usage:
                usage[k] += int(use.get(k) or 0)
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            run.add_step({"n": n, "time": _now(), "kind": "model", "text": msg.get("content") or "",
                          "calls": [{"tool": c["function"]["name"], "args": c["function"].get("arguments")}
                                    for c in calls], "model": model})
            if not calls:
                if toolbox.finished is None and not nudged and not last:
                    nudged = True
                    messages.append({"role": "user", "content": "Continue with the tools, or call finish with "
                                     "the report."})
                    save_state(n)
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
            save_state(n)
            meta.update(steps=n, usage=usage)
            run.save_meta(meta)
            if toolbox.finished is not None:
                break
        if status == "running":
            status = "done" if toolbox.finished else "incomplete"
    except Exception as exc:          # recorded in the run; the service keeps going
        reason = pause_reason(exc)
        status, error = ("paused", reason) if reason else ("error", f"{type(exc).__name__}: {exc}")
        if hasattr(exc, "info"):
            meta["llm"] = exc.info()
        if reason:
            run.add_step({"n": meta.get("steps", 0), "time": _now(), "kind": "pause", "text": reason, "model": model})
    fin = toolbox.finished or {}
    if fin.get("report_md"):
        with open(os.path.join(run.folder, "report.md"), "w", encoding="utf-8") as fp:
            fp.write(fin["report_md"].rstrip() + "\n")
    meta.update(status=status, error=error, finished=_now(),
                seconds=round(float(meta.get("seconds") or 0) * resume + time.monotonic() - started, 1),
                usage=usage, findings=fin.get("findings") or [], confidence=fin.get("confidence"),
                proposals=fin.get("proposals") or [],
                library_used={"skills": dict(toolbox.skills_read),
                              "tools": {**tools_before, **(toolbox.broker.tools_used if toolbox.broker else {})}},
                library_created=list(toolbox.created),
                data_requests=requests_before + (toolbox.broker.requests if toolbox.broker else 0))
    run.save_meta(meta)
    return meta
