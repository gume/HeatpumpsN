"""Optional LLM assistance: proposes descriptions for undescribed signals.

Talks to any OpenAI-compatible ``/chat/completions`` endpoint (LiteLLM, a
local server, a hosted API). It only proposes: the answers are validated,
written to ``proposals.yaml`` and merged into the registry only when the
user accepts them. Without an LLM configured, everything else works.
"""
from __future__ import annotations

import json
import os

import httpx

from .registry import FIELDS

QUANTITIES = ("temperature", "humidity", "power", "energy", "state", "position", "angle", "pressure",
              "modulation", "counter", "duration", "signal_strength", "battery", "illuminance",
              "motion", "contact", "setpoint", "status", "text", "other")
ROLES = ("supply", "return", "room", "outdoor", "boiler", "dhw", "pump", "valve", "burner",
         "controller", "diagnostic", "comfort", "other")

SYSTEM = """You help maintain the sensor registry of a home heating supervisor.
For each signal you get its id (MQTT topic, optionally #json.path), metadata
from the message stream and recent values. Propose a description using the
house context. Be conservative: if you cannot tell, say so with low confidence
and store=false. Answer only with JSON: {"proposals": [ ... ]}, one object per
signal with the keys: signal, name, quantity, unit, location, circuit, role,
description, store (bool), confidence (0..1).
quantity is one of: %s.
role is one of: %s.
Keep name short (as on a dashboard); description is one sentence.""" % (", ".join(QUANTITIES), ", ".join(ROLES))


class LlmClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, cfg: dict | None) -> "LlmClient | None":
        if not cfg or not cfg.get("base_url") or not cfg.get("model"):
            return None
        key = os.environ.get(cfg.get("api_key_env", ""), "") or None
        return cls(cfg["base_url"], cfg["model"], key, float(cfg.get("timeout_s", 120)))

    def chat(self, system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def build_request(signals: dict[str, dict], context: str, known: dict[str, dict]) -> str:
    """User message: house context, already described signals as examples, the signals to describe."""
    examples = dict(list(known.items())[:25])
    return json.dumps({
        "house_context": context.strip(),
        "already_described_examples": examples,
        "signals_to_describe": signals,
    }, ensure_ascii=False, indent=1)


def parse_answer(text: str, wanted: set[str]) -> list[dict]:
    """Validated proposals from the model's answer; anything malformed is dropped."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    out = []
    for item in data.get("proposals", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("signal") not in wanted:
            continue
        clean = {k: item[k] for k in FIELDS if isinstance(item.get(k), str) and item[k].strip()}
        if clean.get("quantity") not in QUANTITIES:
            clean.pop("quantity", None)
        if clean.get("role") not in ROLES:
            clean.pop("role", None)
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({"signal": item["signal"], **clean, "store": bool(item.get("store", False)),
                    "confidence": round(confidence, 2)})
    return out


def propose(client: LlmClient, signals: dict[str, dict], context: str, known: dict[str, dict],
            batch: int = 30) -> list[dict]:
    """Proposals for `signals` ({id: catalog summary}), asked in batches."""
    proposals: list[dict] = []
    items = list(signals.items())
    for i in range(0, len(items), batch):
        chunk = dict(items[i:i + batch])
        answer = client.chat(SYSTEM, build_request(chunk, context, known))
        proposals.extend(parse_answer(answer, set(chunk)))
    return proposals
