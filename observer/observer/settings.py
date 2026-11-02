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
    "harvest": "Find reusable tools and skills in a finished run (1-2 requests; benefits from a strong model)",
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
    def __init__(self, path: str, timeout_s: float = 300.0):
        self.path = path
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self.data = {"providers": {}, "tasks": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                loaded = yaml.safe_load(fp) or {}
            self.data["providers"] = loaded.get("providers") or {}
            self.data["tasks"] = loaded.get("tasks") or {}
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
        return {"providers": providers, "tasks": tasks}

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
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(self.data, fp, sort_keys=False, allow_unicode=True)
        return self.view()

    def client(self, task: str) -> LlmClient | None:
        """The configured client for a task, or None when its provider or model is missing."""
        t = self.data["tasks"].get(task) or {}
        slot, model = str(t.get("provider") or ""), t.get("model") or ""
        info = provider_slots().get(slot)
        if not info or not model:
            return None
        return LlmClient(info["base_url"], model, info["key"], self.timeout_s)

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
