"""Turn MQTT messages into signals.

A signal is one measured or reported quantity. Its id is the topic for a
scalar payload, or ``<topic>#<path>`` for a field of a JSON payload
(``ems-esp/boiler_data#dhw.curtemp``). Values are normalised to a float
(numbers, on/off states) or kept as text; empty and "unknown" values are
dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# Topics that never carry measurements: discovery configs, bridge internals,
# debug streams. Discovery configs are read separately for metadata.
_SKIP_PREFIXES = ("zigbee2mqtt/bridge/", "esphome/")
_SKIP_SUFFIXES = ("/config", "/debug", "/set", "/get", "/availability")
_SKIP_TOPICS = {"ems-esp/info"}

_TRUE = {"on", "true", "yes", "open", "online", "detected"}
_FALSE = {"off", "false", "no", "closed", "offline", "clear"}
_MISSING = {"", "unknown", "unavailable", "none", "null", "nan"}


@dataclass(frozen=True)
class Sample:
    signal: str
    value: float | None
    text: str | None


def normalise(raw: object) -> tuple[float | None, str | None] | None:
    """(value, text) for one scalar, or None when it carries no information."""
    if isinstance(raw, bool):
        return (1.0 if raw else 0.0), None
    if isinstance(raw, (int, float)):
        return float(raw), None
    if raw is None:
        return None
    text = str(raw).strip()
    low = text.lower()
    if low in _MISSING:
        return None
    if low in _TRUE:
        return 1.0, None
    if low in _FALSE:
        return 0.0, None
    try:
        return float(text), None
    except ValueError:
        return None, text


def _flatten(prefix: str, data: object, out: dict[str, object]) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), value, out)
    elif not isinstance(data, list):
        out[prefix] = data


def is_measurement_topic(topic: str) -> bool:
    if topic in _SKIP_TOPICS or topic.startswith(_SKIP_PREFIXES) or topic.endswith(_SKIP_SUFFIXES):
        return False
    return not topic.startswith("homeassistant/")


def parse(topic: str, payload: bytes) -> list[Sample]:
    """All samples in one message (empty for non-measurement topics)."""
    if not is_measurement_topic(topic):
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            fields: dict[str, object] = {}
            _flatten("", data, fields)
            samples = []
            for path, raw in fields.items():
                norm = normalise(raw)
                if norm is not None:
                    samples.append(Sample(f"{topic}#{path}", *norm))
            return samples
    norm = normalise(stripped)
    return [Sample(topic, *norm)] if norm is not None else []
