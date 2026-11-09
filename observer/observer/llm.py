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


_DURATION = __import__("re").compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?$")
_RETRY_TEXT = __import__("re").compile(r"retry (?:in|after) ((?:\d+(?:\.\d+)?[hms])+)", __import__("re").I)
_RETRY_DELAY = __import__("re").compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _duration_s(text: str) -> float | None:
    m = _DURATION.match(text.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def retry_after_s(response) -> float | None:
    """How long the provider asks to wait after a 429, in seconds, or None if it does not say.

    Looks at the Retry-After header, Google's RetryInfo ("retryDelay": "37s") and
    the message text ("Please retry in 6h11m16.5s")."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                return max(0.0, parsedate_to_datetime(header).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    text = response.text or ""
    m = _RETRY_DELAY.search(text)
    if m:
        return float(m.group(1))
    m = _RETRY_TEXT.search(text)
    return _duration_s(m.group(1)) if m else None


_QUOTA_WORDS = ("exceeded your current quota", "per day", "perday", "billing", "limit: 0", "daily")


def quota_exhausted(response) -> bool:
    """A 429 that means the plan's quota is used up (retrying soon is pointless),
    as opposed to a busy or overloaded provider."""
    text = (response.text or "").lower()
    return any(w in text for w in _QUOTA_WORDS)


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60)} min"


class LlmUnavailable(Exception):
    """The model cannot answer now: quota used up, rate limited, busy, no answer in time,
    unreachable, or the request was rejected (wrong model or key). The portal offers
    to retry, to wait, or to switch the model for the task."""

    def __init__(self, kind: str, message: str, model: str = "", task: str = "",
                 retry_after_s: float | None = None, status: int | None = None):
        super().__init__(message)
        self.kind, self.message, self.model, self.task = kind, message, model, task
        self.retry_after_s, self.status = retry_after_s, status

    def info(self) -> dict:
        return {"kind": self.kind, "message": self.message, "model": self.model, "task": self.task,
                "retry_after_s": self.retry_after_s, "status": self.status}

    def __str__(self) -> str:
        when = f"; the provider says retry in {human_duration(self.retry_after_s)}" if self.retry_after_s else ""
        return f"{self.model or 'the model'}: {self.message}{when}"


def _error_text(r) -> str:
    """The provider's message from an error response (OpenAI or Google style), else the raw text."""
    text = r.text or ""
    try:
        body = json.loads(text)
        body = body[0] if isinstance(body, list) and body else body
        text = (body.get("error") or {}).get("message") or text
    except (ValueError, AttributeError):
        pass
    return " ".join(text.split())[:300]


def _merge(dst: dict, src: dict) -> None:
    """Merge one streamed delta into the message being assembled."""
    for k, v in src.items():
        if k == "index":
            continue
        if isinstance(v, dict):
            _merge(dst.setdefault(k, {}), v)
        elif k in ("arguments", "content") and isinstance(v, str):
            dst[k] = (dst.get(k) or "") + v
        elif v is not None:
            dst[k] = v


class LlmClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout_s: float = 120.0,
                 stream: bool = False, task: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.stream = stream
        self.task = task

    @classmethod
    def from_config(cls, cfg: dict | None) -> "LlmClient | None":
        if not cfg or not cfg.get("base_url") or not cfg.get("model"):
            return None
        key = os.environ.get(cfg.get("api_key_env", ""), "") or None
        return cls(cfg["base_url"], cfg["model"], key, float(cfg.get("timeout_s", 120)))

    RETRY_STATUS = (429, 500, 502, 503, 504)
    MAX_SHORT_WAIT_S = 60          # a 429 that asks for at most this long is waited out
    MAX_SHORT_WAITS = 5
    BUSY_WAITS_S = (5, 10, 20, 30)  # busy / overloaded / unreachable: short waits, then give up
    sleep = staticmethod(time.sleep)

    def _unavailable(self, kind: str, message: str, retry_after_s: float | None = None,
                     status: int | None = None) -> LlmUnavailable:
        return LlmUnavailable(kind, message, self.model, self.task, retry_after_s, status)

    def _timeout(self) -> httpx.Timeout:
        # read = the longest silence: between two streamed pieces, or (not streamed) the whole answer
        return httpx.Timeout(connect=15, read=self.timeout_s, write=30, pool=15)

    def _once(self, body: dict, headers: dict):
        """One request: the response JSON (streamed answers assembled), or the error response."""
        url = f"{self.base_url}/chat/completions"
        if not self.stream:
            r = httpx.post(url, json=body, headers=headers, timeout=self._timeout())
            return r.json() if r.status_code == 200 else r
        with httpx.stream("POST", url, json={**body, "stream": True}, headers=headers,
                          timeout=self._timeout()) as r:
            if r.status_code != 200:
                r.read()
                return r
            msg: dict = {"role": "assistant"}
            calls: list[dict] = []
            usage: dict = {}
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for tc in delta.pop("tool_calls", None) or []:
                        i = tc.get("index")
                        if i is None:   # some providers omit the index: a new id starts a new call
                            i = len(calls) if (tc.get("id") or not calls) and not any(
                                c.get("id") == tc.get("id") for c in calls) else len(calls) - 1
                        while len(calls) <= i:
                            calls.append({})
                        _merge(calls[i], tc)
                    _merge(msg, delta)
            if calls:
                msg["tool_calls"] = calls
            return {"choices": [{"message": msg}], "usage": usage}

    def _post(self, body: dict, attempts: int = 3) -> dict:
        """POST /chat/completions; raises LlmUnavailable when the model cannot answer now.

        * no answer within timeout_s (of silence, when streamed): raised at once,
          never retried (it would only use more quota);
        * 429 with a stated delay of at most a minute: waited out and repeated (up
          to MAX_SHORT_WAITS times); a longer delay or a used-up quota: raised at once;
        * busy (429 without time, 5xx) or unreachable: the short waits of
          BUSY_WAITS_S, then raised;
        * 401/403/404 (key or model rejected): raised at once."""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        busy = short_waits = 0
        while True:
            try:
                r = self._once(body, headers)
            except httpx.TimeoutException:
                what = "of silence" if self.stream else ""
                raise self._unavailable("timeout", f"no answer within {self.timeout_s:.0f} s {what}".strip()) from None
            except httpx.TransportError as exc:
                if busy >= len(self.BUSY_WAITS_S):
                    raise self._unavailable("unreachable", f"endpoint not reachable: {exc}") from None
                self.sleep(self.BUSY_WAITS_S[busy])
                busy += 1
                continue
            if isinstance(r, dict):
                return r
            if self.stream and r.status_code in (400, 422) and "stream" in r.text.lower():
                self.stream = False                       # the provider does not stream: ask again plainly
                continue
            if r.status_code == 429:
                delay = retry_after_s(r)
                if delay is not None:
                    if delay <= self.MAX_SHORT_WAIT_S and short_waits < self.MAX_SHORT_WAITS:
                        short_waits += 1
                        self.sleep(delay + 1)
                        continue
                    raise self._unavailable("quota", _error_text(r), delay, 429)
                if quota_exhausted(r):
                    raise self._unavailable("quota", _error_text(r), None, 429)
            if r.status_code in (401, 403, 404):
                raise self._unavailable("rejected", f"HTTP {r.status_code}: {_error_text(r)}", None, r.status_code)
            if r.status_code not in self.RETRY_STATUS:
                r.raise_for_status()
            if busy >= len(self.BUSY_WAITS_S):
                raise self._unavailable("busy", f"HTTP {r.status_code}: {_error_text(r)}", None, r.status_code)
            self.sleep(self.BUSY_WAITS_S[busy])
            busy += 1

    def chat(self, system: str, user: str, attempts: int = 3) -> str:
        """One chat completion; transient errors (overload, rate limit) are retried."""
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        return self._post(body, attempts)["choices"][0]["message"]["content"]

    def chat_tools(self, messages: list[dict], tools: list[dict], attempts: int = 3) -> tuple[dict, dict]:
        """One step of function calling: (assistant message as returned, usage).

        The message goes back into the history unchanged: some providers
        (Gemini) attach signatures to tool calls that must be returned."""
        body = {"model": self.model, "temperature": 0, "messages": messages}
        if tools:
            body["tools"] = tools
        data = self._post(body, attempts)
        return data["choices"][0]["message"], data.get("usage") or {}


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


# --- The house description draft ---------------------------------------------

HOUSE_UPDATE_SYSTEM = """You maintain the description of a house's heating system for its
owner and for a supervisor that later analyses the data. You get the house
context, the current description, the previous version, the owner's new input,
the described signals (id, name, quantity, unit, location, circuit, role,
description) and signals that are recorded or seen but not described or not
stored (candidates).

Update the description:
- The owner's input is authoritative. On any conflict with the current or the
  previous description, follow the owner's input.
- Keep everything in the current description that the input does not
  contradict or replace.
- The owner may refer to the previous version (for example "bring back the
  radiators from the previous version"). Restore or reuse facts from the
  previous version when the input asks for it.
- Do not invent equipment the inputs do not suggest; say what is uncertain.

Answer only with JSON:
{
 "description_md": "Markdown for the owner. Sections: ## Overview, ## Boiler and hot water, ## Heating circuits (one ### per circuit), ## Rooms, ## Outdoor, ## Controller and measurement devices, ## How the parts interact (expected behaviour, with typical numbers where you can estimate them), ## Uncertain points. Name sensors by their names, with the id in backticks the first time. Plain, concrete sentences.",
 "model": {"systems": [{"name", "description", "signals": []}],
           "points": [{"signal", "measures", "use"}],
           "relations": [{"type": "redundant"|"order"|"causal"|"balance", "signals": [], "condition", "expectation", "why"}],
           "checks": [{"name", "signals": [], "condition", "severity": "info"|"warning"|"alarm"}],
           "gaps": []},
 "notes": "short summary of what you changed and which input you followed"
}
Use only signal ids from the input in "model". Be physically plausible."""


def update_house_description(client: LlmClient, context: str, described: dict[str, dict],
                             candidates: dict[str, dict], current_md: str, previous_md: str,
                             user_input: str) -> dict:
    """Merge the owner's input into the current description (validated, not saved)."""
    user = json.dumps({"house_context": context.strip(),
                       "current_description": current_md.strip(),
                       "previous_description": previous_md.strip(),
                       "user_input": user_input.strip(),
                       "described_signals": described, "candidate_signals": candidates},
                      ensure_ascii=False, indent=1)
    data = chat_json(client, HOUSE_UPDATE_SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("description_md"), str) or not data["description_md"].strip():
        raise ValueError("the answer has no description")
    import re
    known = set(described) | set(candidates)
    # Signal ids the prose names in backticks but that do not exist.
    named = {m for m in re.findall(r"`([^`\s]+)`", data["description_md"]) if "/" in m}
    return {"description_md": data["description_md"].strip(),
            "unknown_ids": sorted(named - known),
            "notes": data["notes"].strip() if isinstance(data.get("notes"), str) else "",
            "model": validate_model(data.get("model") or {}, set(described))}


HARVEST_SYSTEM = """You review a finished analysis run of a home heating observer and
propose what should become reusable: analysis TOOLS (code) and SKILLS
(recipes). The library already has the items listed; do not duplicate them
(propose an improved version only with a clearly different name and why).

A tool must be universal: it works on generic inputs (pandas Series with a
DatetimeIndex, DataFrames, numbers), never on one house's signal ids, so it
can move to another house with other sensors or no controller. Write it as a
Python module with a function run(...) (numpy, pandas, scipy allowed), a
clear purpose, the kind of data it needs, typed inputs and the output, and
pytest tests on synthetic data (`from tool import run`, functions test_...)
that check the result numerically. Generalise the code the run wrote; fix its
bugs; keep it small.

A skill is a recipe in Markdown: the steps that worked in this run, in
generic terms (circuits, roles, regimes), the checks and thresholds used, the
pitfalls met, and the library tools to use. Propose one only if the method is
worth repeating.

Propose nothing rather than something weak. Answer only with JSON:
{"tools": [{"name": "snake_case", "purpose": "...", "needs": "...",
            "inputs": [{"name", "type": "series|dataframe|number|integer|string|boolean|list|dict|timestamp|any",
                        "description", "required": true}],
            "outputs": {"type", "description"},
            "tests_description": ["one plain sentence per test: what it checks, e.g. 'an off-gap shorter than min_off_s is merged'"],
            "code": "...", "tests": "...", "why": "what in the run it comes from"}],
 "skills": [{"name": "snake_case", "title", "description", "recipe": "Markdown", "tools": ["names"],
             "why": "..."}],
 "notes": "one or two sentences about the run's method"}"""


def harvest_run(client: LlmClient, digest: dict, library: dict) -> dict:
    """Proposed tools and skills from a run digest; incomplete proposals are dropped."""
    user = json.dumps({"run": digest, "library": library}, ensure_ascii=False, indent=1)
    data = chat_json(client, HARVEST_SYSTEM, user)
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    import re
    name_ok = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
    tools = [t for t in data.get("tools") or [] if isinstance(t, dict) and name_ok.match(str(t.get("name", "")))
             and "def run(" in str(t.get("code", "")) and "def test_" in str(t.get("tests", ""))
             and isinstance(t.get("outputs"), dict) and t.get("purpose")]
    for t in tools:
        desc = [str(d).strip() for d in t.get("tests_description") or [] if str(d).strip()]
        if not desc:      # from the test function names: test_merges_short_gaps -> "merges short gaps"
            desc = [n.replace("_", " ") for n in re.findall(r"def test_(\w+)", t["tests"])]
        t["tests_description"] = desc
    skills = [k for k in data.get("skills") or [] if isinstance(k, dict) and name_ok.match(str(k.get("name", "")))
              and k.get("recipe") and k.get("description")]
    return {"tools": tools, "skills": skills, "notes": str(data.get("notes") or "")}


TOOL_WRITER_SYSTEM = """You write one analysis tool for a home heating observer from its
description. The tool runs in a sandbox with numpy, pandas and scipy, and must
be universal: generic inputs (pandas Series with a DatetimeIndex, DataFrames,
numbers), never one house's signal ids.

You get the description: name, purpose, the data it needs, the inputs (name,
type, description, required, default), the output, and the tests as plain
sentences. Previous code may be given: keep what still fits the description.

Write
* "code": a Python module with a function run(...) whose parameters are
  exactly the inputs, in that order, with the defaults given; it returns the
  output described. Small, readable, no I/O, no network.
* "tests": a pytest module (`from tool import run`) with one test function per
  test sentence, in the same order, on synthetic data, checking results
  numerically.

Answer only with JSON: {"code": "...", "tests": "..."}"""


def write_tool(client: LlmClient, spec: dict, code: str = "", tests: str = "") -> dict:
    """Code and tests for a tool description (the tool editor's "write code from the description")."""
    keys = ("name", "purpose", "needs", "inputs", "outputs", "tests_description")
    user = json.dumps({"description": {k: spec.get(k) for k in keys}, "previous_code": code or None,
                       "previous_tests": tests or None}, ensure_ascii=False, indent=1)
    data = chat_json(client, TOOL_WRITER_SYSTEM, user)
    if not isinstance(data, dict) or "def run(" not in str(data.get("code", "")) \
            or "def test_" not in str(data.get("tests", "")):
        raise ValueError("the answer has no run() function or no tests")
    return {"code": data["code"].rstrip() + "\n", "tests": data["tests"].rstrip() + "\n"}
