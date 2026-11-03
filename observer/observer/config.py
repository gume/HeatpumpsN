"""The observer's configuration: config.yaml in the data directory, .env, environment overrides.

Shared by the portal and the data collector (whose image contains only the
collector's modules, see Dockerfile.collector and collector.lock).
"""
from __future__ import annotations

import os
import sys

import yaml


def observer_home() -> str:
    return os.path.abspath(os.path.expanduser(
        os.environ.get("OBSERVER_HOME") or "~/.local/share/heatpumpsn-observer"))


def load_env(path: str) -> None:
    """KEY=VALUE lines from a .env file into the environment.

    A variable already set in the environment wins, unless it is empty: docker
    compose passes every listed variable, empty when deploy/.env leaves it out,
    and that must not hide the value in the data directory's .env."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not os.environ.get(key):
                os.environ[key] = value.strip().strip('"').strip("'")


ENV_OVERRIDES = (("OBSERVER_MQTT_HOST", "mqtt", "host", str), ("OBSERVER_MQTT_PORT", "mqtt", "port", int),
                 ("OBSERVER_MQTT_USER", "mqtt", "username", str),
                 ("OBSERVER_INFLUX_URL", "influx", "url", str), ("OBSERVER_INFLUX_ORG", "influx", "org", str),
                 ("OBSERVER_INFLUX_BUCKET", "influx", "bucket", str))


def apply_env_overrides(cfg: dict) -> None:
    """Machine-specific settings from the environment win over config.yaml (docker compose)."""
    for var, section, key, conv in ENV_OVERRIDES:
        if os.environ.get(var, "").strip():
            cfg.setdefault(section, {})[key] = conv(os.environ[var].strip())
    if os.environ.get("OBSERVER_MQTT_PASSWORD"):
        cfg.setdefault("mqtt", {})["password_env"] = "OBSERVER_MQTT_PASSWORD"
    if "OBSERVER_MQTT_TOPIC_PREFIX" in os.environ:
        prefix = os.environ["OBSERVER_MQTT_TOPIC_PREFIX"].strip()
        cfg.setdefault("mqtt", {}).update(topic_prefix=prefix, subscribe=[prefix + "#"])


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(f"no configuration at {path}: run `observer init` (data directory: OBSERVER_HOME)")
    base = os.path.dirname(os.path.abspath(path))
    load_env(os.path.join(base, ".env"))
    with open(path, encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    cfg["llm"] = cfg.get("llm") or {}   # providers come from .env, models from the Settings page
    apply_env_overrides(cfg)
    paths = cfg.setdefault("paths", {})
    for key, default in (("catalog", "data/catalog.json"), ("registry", "registry.yaml"),
                         ("proposals", "data/proposals.yaml"), ("house_dir", "house/house.keep"),
                         ("house_draft", "data/house_draft.yaml"), ("settings", "data/settings.yaml"),
                         ("runs", "runs/runs.keep"), ("library", "library/library.keep"),
                         ("sandbox_venv", "sandbox/venv"), ("exports", "exports/exports.keep"),
                         ("checks", "checks.yaml")):
        value = paths.get(key, default)
        paths[key] = value if os.path.isabs(value) else os.path.join(base, value)
        os.makedirs(os.path.dirname(paths[key]), exist_ok=True)
    paths["house_dir"] = os.path.dirname(paths["house_dir"])   # a folder: house/
    if os.environ.get("OBSERVER_SANDBOX_VENV"):                # the container image brings its own
        paths["sandbox_venv"] = os.environ["OBSERVER_SANDBOX_VENV"]
    cfg["config_path"] = os.path.abspath(path)
    old_draft = os.path.join(os.path.dirname(paths["house_draft"]), "house_proposal.yaml")
    if os.path.exists(old_draft) and not os.path.exists(paths["house_draft"]):
        os.replace(old_draft, paths["house_draft"])             # the draft's former name
    paths["runs"] = os.path.dirname(paths["runs"])             # agent runs: runs/<id>/
    paths["exports"] = os.path.dirname(paths["exports"])       # database exports (.parquet)
    paths["library"] = os.path.dirname(paths["library"])       # house tools and skills, audits
    return cfg


def mqtt_args(cfg: dict) -> dict:
    m = cfg.get("mqtt", {})
    return {"host": m.get("host", "localhost"), "port": int(m.get("port", 1883)),
            "subscribe": m.get("subscribe", ["#"]), "username": m.get("username"),
            "password": os.environ.get(m.get("password_env", ""), None)}
