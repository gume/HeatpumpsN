"""Metadata the MQTT stream itself carries about signals.

* Home Assistant discovery configs (``homeassistant/<component>/.../config``):
  name, unit, device class and device for the entity's state topic.
* Zigbee2MQTT's device list (``zigbee2mqtt/bridge/devices``): vendor, model,
  description and the unit of every exposed property.
* HA MQTT Statestream topics (``<base>/<domain>/<object>/state``): the HA
  entity id.

Each source yields ``{signal id: {key: value}}``; the catalog merges them.
"""
from __future__ import annotations

import json
import re

_VALUE_JSON = re.compile(r"value_json(?:\.|\[['\"])([A-Za-z0-9_]+)")


def from_discovery(topic: str, payload: bytes) -> dict[str, dict]:
    """Metadata from one HA discovery config message."""
    if not topic.startswith("homeassistant/") or not topic.endswith("/config"):
        return {}
    try:
        cfg = json.loads(payload)
    except ValueError:
        return {}
    if not isinstance(cfg, dict):
        return {}
    state_topic = cfg.get("state_topic") or cfg.get("stat_t")
    if not state_topic:
        return {}
    signal = state_topic
    template = cfg.get("value_template") or cfg.get("val_tpl") or ""
    match = _VALUE_JSON.search(template)
    if match:
        signal = f"{state_topic}#{match.group(1)}"
    device = cfg.get("device") or cfg.get("dev") or {}
    meta = {
        "name": cfg.get("name"),
        "unit": cfg.get("unit_of_measurement") or cfg.get("unit_of_meas"),
        "device_class": cfg.get("device_class") or cfg.get("dev_cla"),
        "component": topic.split("/")[1],
        "device": device.get("name") or device.get("model"),
        "device_model": device.get("model"),
        "diagnostic": (cfg.get("entity_category") or cfg.get("ent_cat")) == "diagnostic",
    }
    return {signal: {k: v for k, v in meta.items() if v not in (None, "", False)}}


def from_z2m_devices(payload: bytes) -> dict[str, dict]:
    """Metadata from ``zigbee2mqtt/bridge/devices``."""
    try:
        devices = json.loads(payload)
    except ValueError:
        return {}
    out: dict[str, dict] = {}
    for dev in devices if isinstance(devices, list) else []:
        if dev.get("type") == "Coordinator":
            continue
        name = dev.get("friendly_name") or dev.get("ieee_address")
        definition = dev.get("definition") or {}
        base = {
            "device": name,
            "device_model": " ".join(x for x in (definition.get("vendor"), definition.get("model")) if x),
            "device_description": definition.get("description"),
        }
        for feature in _features(definition.get("exposes") or []):
            prop = feature.get("property")
            if not prop:
                continue
            meta = dict(base, name=feature.get("label") or prop, unit=feature.get("unit"),
                        description=feature.get("description"))
            out[f"zigbee2mqtt/{name}#{prop}"] = {k: v for k, v in meta.items() if v}
    return out


def _features(exposes: list) -> list[dict]:
    flat: list[dict] = []
    for item in exposes:
        if not isinstance(item, dict):
            continue
        if item.get("features"):
            flat.extend(_features(item["features"]))
        else:
            flat.append(item)
    return flat


def from_statestream(topic: str, base: str) -> dict[str, dict]:
    """``<base>/<domain>/<object>/state`` carries the HA entity id."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != base or parts[3] != "state":
        return {}
    return {topic: {"ha_entity": f"{parts[1]}.{parts[2]}", "component": parts[1]}}
