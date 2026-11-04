"""User settings that are not secrets: LLM provider names and the model per task.

Secrets stay in .env: each provider slot has an endpoint and a key,

    OBSERVER_LLM_BASE_URL / OBSERVER_LLM_KEY          slot 1
    OBSERVER_LLM_2_BASE_URL / OBSERVER_LLM_2_KEY      slot 2   (up to 9)

and the portal's Settings page stores, in data/settings.yaml, a display name
per slot and, for each task, which slot and which model it uses. Models
differ in reasoning strength and cost, so routine work can use a fast model
and the house description a strong one.
"""
from __future__ import annotations

import os
import threading

import yaml

from .llm import LlmClient

TASKS = {
    "describe": "Polish one signal's description",
    "propose": "Describe undescribed signals (bulk proposals)",
    "house": "House description and model (benefits from a strong model)",
    "agent": "Agent runs: skills and questions with tools (benefits from a strong model)",
    "harvest": "Find tools and skills in a run, write tools from their description (benefits from a strong model)",
}
MAX_SLOTS = 9


def provider_slots() -> dict[str, dict]:
    """Provider slots configured in the environment: {slot: {base_url, key}}."""
    slots = {}
    for n in range(1, MAX_SLOTS + 1):
        prefix = "OBSERVER_LLM_" if n == 1 else f"OBSERVER_LLM_{n}_"
        base = os.environ.get(prefix + "BASE_URL", "").strip()
        if base:
            slots[str(n)] = {"base_url": base, "key": os.environ.get(prefix + "KEY", "").strip() or None}
    return slots


class Settings:
    DEFAULT_RESPONSE = {"timeout_s": 120, "stream": True}

    def __init__(self, path: str, timeout_s: float | None = None):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"providers": {}, "tasks": {}, "response": dict(self.DEFAULT_RESPONSE)}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                loaded = yaml.safe_load(fp) or {}
            self.data["providers"] = loaded.get("providers") or {}
            self.data["tasks"] = loaded.get("tasks") or {}
            self.data["response"].update(loaded.get("response") or {})
        # Migration from the single-provider setup: OBSERVER_LLM_MODEL in .env.
        legacy = os.environ.get("OBSERVER_LLM_MODEL", "").strip()
        if legacy:
            for task in TASKS:
                self.data["tasks"].setdefault(task, {"provider": "1", "model": legacy})

    def view(self) -> dict:
        slots = provider_slots()
        providers = [{"slot": slot, "name": (self.data["providers"].get(slot) or {}).get("name") or f"Provider {slot}",
                      "base_url": info["base_url"], "has_key": bool(info["key"])} for slot, info in slots.items()]
        tasks = []
        for task, label in TASKS.items():
            t = self.data["tasks"].get(task) or {}
            tasks.append({"task": task, "label": label, "provider": str(t.get("provider") or ""),
                          "model": t.get("model") or "", "ready": self.client(task) is not None})
        return {"providers": providers, "tasks": tasks, "response": dict(self.data["response"])}

    def update(self, body: dict) -> dict:
        slots = provider_slots()
        with self._lock:
            for slot, info in (body.get("providers") or {}).items():
                if str(slot) in slots and isinstance(info, dict):
                    name = str(info.get("name") or "").strip()
                    self.data["providers"][str(slot)] = {"name": name} if name else {}
            for task, info in (body.get("tasks") or {}).items():
                if task in TASKS and isinstance(info, dict):
                    self.data["tasks"][task] = {"provider": str(info.get("provider") or "").strip(),
                                                "model": str(info.get("model") or "").strip()}
            resp = body.get("response")
            if isinstance(resp, dict):
                if "timeout_s" in resp:
                    t = float(resp["timeout_s"])
                    if not 10 <= t <= 3600:
                        raise ValueError("response timeout: 10 to 3600 seconds")
                    self.data["response"]["timeout_s"] = int(t)
                if "stream" in resp:
                    self.data["response"]["stream"] = bool(resp["stream"])
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(self.data, fp, sort_keys=False, allow_unicode=True)
        return self.view()

    def client(self, task: str, provider: str | None = None, model: str | None = None) -> LlmClient | None:
        """The configured client for a task, or None when its provider or model is missing.

        provider/model: another choice for this one request (e.g. the House tab's model field)."""
        t = self.data["tasks"].get(task) or {}
        if provider or (model or "").strip():           # a choice for this request: both or nothing from settings
            slot, model = str(provider or t.get("provider") or ""), (model or "").strip()
        else:
            slot, model = str(t.get("provider") or ""), t.get("model") or ""
        info = provider_slots().get(slot)
        if not info or not model:
            return None
        r = self.data["response"]
        return LlmClient(info["base_url"], model, info["key"], float(r["timeout_s"]), bool(r["stream"]), task)

    def models(self, slot: str) -> list[str]:
        """Model ids the provider lists (OpenAI-compatible /models)."""
        import httpx

        info = provider_slots().get(str(slot))
        if not info:
            raise KeyError(slot)
        headers = {"Authorization": f"Bearer {info['key']}"} if info["key"] else {}
        r = httpx.get(f"{info['base_url'].rstrip('/')}/models", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json().get("data") or []
        return sorted({str(m.get("id", "")).removeprefix("models/") for m in data if m.get("id")})
