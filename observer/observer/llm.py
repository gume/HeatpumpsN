"""Optional LLM assistance: proposes descriptions for undescribed signals.

Talks to any OpenAI-compatible ``/chat/completions`` endpoint (LiteLLM, a
local server, a hosted API). It only proposes: the answers are validated,
written to ``proposals.yaml`` and merged into the registry only when the
user accepts them. Without an LLM configured, everything else works.
"""
from __future__ import annotations

import json
import os
import time

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

    RETRY_STATUS = (429, 500, 502, 503, 504)

    def chat(self, system: str, user: str, attempts: int = 3) -> str:
        """One chat completion; transient errors (overload, rate limit) are retried."""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(attempts):
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers,
                               timeout=self.timeout_s)
                if r.status_code not in self.RETRY_STATUS or attempt == attempts - 1:
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == attempts - 1:
                    raise
            time.sleep(10 * (attempt + 1))
        raise RuntimeError("unreachable")


_TRAILING_COMMA = __import__("re").compile(r",(\s*[}\]])")


def loads_lenient(text: str):
    """JSON from a model's answer: code fences, surrounding prose and trailing commas tolerated."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the answer contains no JSON object")
    body = text[start:end + 1]
    try:
        return json.loads(body)
    except ValueError:
        return json.loads(_TRAILING_COMMA.sub(r"\1", body))


def chat_json(client, system: str, user: str):
    """Ask for JSON; if the answer does not parse, show the model its error once and ask again."""
    text = client.chat(system, user)
    try:
        return loads_lenient(text)
    except ValueError as exc:
        repair = (f"Your previous answer was not valid JSON ({exc}). Return the same content again as "
                  f"one valid JSON object only, without comments or trailing commas.\n\nPrevious answer:\n{text}")
        return loads_lenient(client.chat(system, repair))


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


# --- Polishing one description -------------------------------------------------

POLISH_SYSTEM = """You polish the description of one sensor signal in a home heating
supervisor's registry. You get the user's draft (it may be terse, in any
language, or partly empty), the signal's id, its metadata from the message
stream, its recent value, the matching rule's defaults, related signals and the
house context. Keep everything the user stated; correct obvious mistakes only
when the evidence is clear. Write the description in English, one or two
sentences: what is measured, where, and what it is useful for. Answer only with
JSON: {"name", "quantity", "unit", "location", "circuit", "role", "description",
"store" (bool), "notes" (what you changed and why, short)}.
quantity is one of: %s.
role is one of: %s.
circuit is one of: floor, rad1, rad2, or empty.""" % (", ".join(QUANTITIES), ", ".join(ROLES))


def polish(client: LlmClient, signal: str, draft: dict, facts: dict, context: str, related: dict) -> dict:
    """A polished registry entry for `signal`, from the user's draft (validated, not saved)."""
    user = json.dumps({"house_context": context.strip(), "signal": signal, "user_draft": draft,
                       "facts": facts, "related_signals": related}, ensure_ascii=False, indent=1)
    data = chat_json(client, POLISH_SYSTEM, user)
    out = {k: data[k].strip() for k in FIELDS if isinstance(data.get(k), str) and data[k].strip()}
    if out.get("quantity") not in QUANTITIES:
        out.pop("quantity", None)
    if out.get("role") not in ROLES:
        out.pop("role", None)
    if out.get("circuit") not in ("floor", "rad1", "rad2"):
        out.pop("circuit", None)
    out["store"] = bool(data.get("store", draft.get("store", True)))
    if isinstance(data.get("notes"), str):
        out["notes"] = data["notes"].strip()
    return out


# --- The house model -------------------------------------------------------------

MODEL_SYSTEM = """You build the sensor model of a house heated by a gas boiler with three
heating circuits, for a supervisor that later analyses the data and detects
anomalies. You get the house context and the list of described signals (id,
name, quantity, unit, location, circuit, role, description). Produce:

1. systems: the physical parts (boiler, DHW, each heating circuit, the floor
   mixer, rooms per floor, outdoor, the controller, the measurement devices),
   each with a short description and the ids of the signals that belong to it.
2. points: for every signal that matters for heating, what it measures
   physically and what it is used for in analysis.
3. relations between signals, each with type one of
     redundant (two signals measure the same quantity; they should agree within a tolerance),
     order (one should be above/below another under a condition),
     causal (a change in one should be followed by a change in another, with a typical delay),
     balance (a quantity is derived from others),
   the signal ids, the condition when it holds, the expectation (with numbers
   where you can estimate them) and why.
4. checks: anomaly checks that follow from the relations and from single
   signals (staleness, stuck values, out-of-range, short-cycling), each with
   the signal ids, the condition and a severity (info, warning, alarm).

Use only signal ids from the list. Be concrete and physically plausible;
leave out what you cannot justify. Answer only with JSON:
{"systems": [{"name", "description", "signals": []}],
 "points": [{"signal", "measures", "use"}],
 "relations": [{"type", "signals": [], "condition", "expectation", "why"}],
 "checks": [{"name", "signals": [], "condition", "severity"}],
 "gaps": ["what is missing for a good analysis (sensors, descriptions)"]}"""

RELATION_TYPES = ("redundant", "order", "causal", "balance")
SEVERITIES = ("info", "warning", "alarm")


def build_house_model(client: LlmClient, signals: dict[str, dict], context: str) -> dict:
    """A validated house model from the described signals ({id: description})."""
    user = json.dumps({"house_context": context.strip(), "signals": signals}, ensure_ascii=False, indent=1)
    return validate_model(chat_json(client, MODEL_SYSTEM, user), set(signals))


def validate_model(data: dict, known: set[str]) -> dict:
    """Keep only well-formed entries that reference known signals."""
    def ids(item):
        return [s for s in item.get("signals", []) if isinstance(s, str) and s in known]

    def text(item, key):
        value = item.get(key)
        return value.strip() if isinstance(value, str) else ""

    model = {"systems": [], "points": [], "relations": [], "checks": [], "gaps": []}
    for item in data.get("systems", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and text(item, "name"):
            model["systems"].append({"name": text(item, "name"), "description": text(item, "description"),
                                     "signals": ids(item)})
    for item in data.get("points", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("signal") in known:
            model["points"].append({"signal": item["signal"], "measures": text(item, "measures"),
                                    "use": text(item, "use")})
    for item in data.get("relations", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("type") in RELATION_TYPES and len(ids(item)) >= 2:
            model["relations"].append({"type": item["type"], "signals": ids(item),
                                       "condition": text(item, "condition"),
                                       "expectation": text(item, "expectation"), "why": text(item, "why")})
    for item in data.get("checks", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and text(item, "name") and ids(item):
            severity = item.get("severity") if item.get("severity") in SEVERITIES else "warning"
            model["checks"].append({"name": text(item, "name"), "signals": ids(item),
                                    "condition": text(item, "condition"), "severity": severity})
    for gap in data.get("gaps", []) if isinstance(data, dict) else []:
        if isinstance(gap, str) and gap.strip():
            model["gaps"].append(gap.strip())
    return model


# --- The reviewed house description ------------------------------------------

HOUSE_SYSTEM = """You write the description of a house's heating system for its owner,
who will review it, correct it and add facts. A supervisor later uses the
same understanding for analysis and anomaly detection.

You get: the house context, the described signals (id, name, quantity, unit,
location, circuit, role, description), signals that are recorded or seen but
not described or not stored (candidates), the owner's review notes (they are
authoritative: follow them, even against your own reading) and the previous
audited description (keep what is still right, improve the rest).

Answer only with JSON:
{
 "description_md": "Markdown for the owner. Sections: ## Overview, ## Boiler and hot water, ## Heating circuits (one ### per circuit), ## Rooms, ## Outdoor, ## Controller and measurement devices, ## How the parts interact (expected behaviour, with typical numbers where you can estimate them), ## Uncertain points. Name sensors by their names, with the id in backticks the first time. Plain, concrete sentences.",
 "questions": ["specific questions whose answers would make the description or the analysis better"],
 "recommendations": [{"kind": "existing_signal" or "new_sensor", "signal": "candidate id (existing_signal only)", "what": "short", "why": "what it would enable", "priority": "high" | "medium" | "low"}],
 "model": {"systems": [{"name", "description", "signals": []}],
           "points": [{"signal", "measures", "use"}],
           "relations": [{"type": "redundant"|"order"|"causal"|"balance", "signals": [], "condition", "expectation", "why"}],
           "checks": [{"name", "signals": [], "condition", "severity": "info"|"warning"|"alarm"}],
           "gaps": []}
}
Use only signal ids from the input in "model" and "signal". Be physically
plausible; do not invent equipment the inputs do not suggest; say what is
uncertain instead."""

PRIORITIES = ("high", "medium", "low")


def build_house_description(client: LlmClient, context: str, described: dict[str, dict],
                            candidates: dict[str, dict], notes: str, previous_md: str) -> dict:
    """Validated description, questions, recommendations and model from the LLM."""
    user = json.dumps({"house_context": context.strip(), "owner_review_notes": notes.strip(),
                       "previous_audited_description": previous_md.strip(),
                       "described_signals": described, "candidate_signals": candidates},
                      ensure_ascii=False, indent=1)
    data = chat_json(client, HOUSE_SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("description_md"), str) or not data["description_md"].strip():
        raise ValueError("the answer has no description")
    known = set(described) | set(candidates)
    recs = []
    for r in data.get("recommendations", []):
        if not isinstance(r, dict) or r.get("kind") not in ("existing_signal", "new_sensor"):
            continue
        if r["kind"] == "existing_signal" and r.get("signal") not in known:
            continue
        recs.append({"kind": r["kind"], "signal": r.get("signal") if r["kind"] == "existing_signal" else "",
                     "what": str(r.get("what") or "").strip(), "why": str(r.get("why") or "").strip(),
                     "priority": r.get("priority") if r.get("priority") in PRIORITIES else "medium"})
    # Signal ids the prose names in backticks but that do not exist (a model's
    # typo would otherwise pass a human review unnoticed).
    import re
    named = {m for m in re.findall(r"`([^`\s]+)`", data["description_md"]) if "/" in m}
    unknown = sorted(named - known)
    return {"description_md": data["description_md"].strip(),
            "unknown_ids": unknown,
            "questions": [q.strip() for q in data.get("questions", []) if isinstance(q, str) and q.strip()],
            "recommendations": recs,
            "model": validate_model(data.get("model") or {}, set(described))}
