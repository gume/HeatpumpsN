"""The data collector: MQTT in, InfluxDB out, nothing else (``observer collect``).

It runs as its own process (its own container in the release), so the portal
(web, checks, agents, parameters) can be restarted and updated without a gap
in the recorded data. It shares the data directory with the portal:

* it reads ``registry.yaml`` (which signals are stored, their tags) and picks up
  the portal's edits within seconds;
* it writes its state to ``data/collector.json`` every 10 s (connected,
  messages, samples written, last write); the portal shows it, counts the
  stored samples from it and raises an alarm when the file goes stale.

It keeps no catalog of its own (the portal owns ``data/catalog.json``) and
ignores the observer's own publications (``<alerts.mqtt_topic>/...``).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

from . import __version__
from .catalog import Catalog
from .ingest import Ingest, MqttRunner
from .registry import Registry
from .store import InfluxWriter

log = logging.getLogger("observer.collector")
STATUS_FILE = "collector.json"


def status_path(cfg: dict) -> str:
    return os.path.join(os.path.dirname(cfg["paths"]["catalog"]), STATUS_FILE)


def read_status(cfg: dict) -> dict | None:
    path = status_path(cfg)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def run(cfg: dict, mqtt_args: dict) -> None:
    i = cfg.get("influx") or {}
    token = os.environ.get(i.get("token_env", "INFLUX_TOKEN"))
    if not token:
        raise SystemExit(f"InfluxDB token missing: set {i.get('token_env', 'INFLUX_TOKEN')}")
    writer = InfluxWriter(i["url"], token, i["org"], i["bucket"])
    m = cfg.get("mqtt", {})
    reg_path = cfg["paths"]["registry"]
    ingest = Ingest(Catalog(), Registry(reg_path), writer, prefix=m.get("topic_prefix", ""),
                    statestream_base=cfg.get("statestream_base", "ha_state"))
    ingest.ignore = (((cfg.get("alerts") or {}).get("mqtt_topic") or "observer").strip("/") + "/",)
    lock = threading.Lock()

    def handle(topic, payload, retained):
        with lock:
            ingest.handle(topic, payload, retained)

    runner = MqttRunner(handle, **mqtt_args)
    runner.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):        # docker stop: flush the last batch, then leave
        signal.signal(sig, lambda *_: stop.set())
    started = time.time()
    reg_mtime = os.path.getmtime(reg_path) if os.path.exists(reg_path) else 0
    last_stored, last_write = 0, None
    log.info("collector %s: %s:%s -> InfluxDB %s", __version__, mqtt_args["host"], mqtt_args["port"], i["url"])
    try:
        while not stop.wait(10):
            mtime = os.path.getmtime(reg_path) if os.path.exists(reg_path) else 0
            if mtime != reg_mtime:                     # the portal saved the registry
                reg_mtime = mtime
                with lock:
                    ingest.registry = Registry(reg_path)
                log.info("registry reloaded")
            if ingest.stored != last_stored:
                last_stored, last_write = ingest.stored, time.time()
            status = {"updated": time.time(), "started": started, "version": __version__, "pid": os.getpid(),
                      "connected": runner.connected, "host": runner.host, "port": runner.port,
                      "messages": runner.messages, "last_message": runner.last_message,
                      "stored": ingest.stored, "last_write": last_write}
            path = status_path(cfg)
            with open(path + ".tmp", "w", encoding="utf-8") as fp:
                json.dump(status, fp)
            os.replace(path + ".tmp", path)
    finally:
        runner.stop()
        writer.close()
        log.info("collector stopped after %d samples", ingest.stored)
