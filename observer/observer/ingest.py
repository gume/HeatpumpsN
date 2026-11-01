"""From MQTT messages to catalog updates and stored samples."""
from __future__ import annotations

import logging
import time

from . import metadata
from .catalog import Catalog
from .payload import parse
from .registry import Registry
from .store import Point

log = logging.getLogger(__name__)


class Ingest:
    """Handles one message at a time; the MQTT loop (run_mqtt) feeds it."""

    def __init__(self, catalog: Catalog, registry: Registry, writer, prefix: str = "",
                 statestream_base: str = "ha_state"):
        self.catalog, self.registry, self.writer = catalog, registry, writer
        self.prefix = prefix
        self.statestream_base = statestream_base
        self.stored = 0
        self.new_signals: list[str] = []

    def handle(self, topic: str, payload: bytes, retained: bool = False, time_ns: int | None = None) -> None:
        if self.prefix:
            if not topic.startswith(self.prefix):
                return
            topic = topic[len(self.prefix):]
        time_ns = time.time_ns() if time_ns is None else time_ns

        # Metadata the stream carries about signals.
        if topic.startswith("homeassistant/") and topic.endswith("/config"):
            self.catalog.add_meta(metadata.from_discovery(topic, payload))
            return
        if topic == "zigbee2mqtt/bridge/devices":
            self.catalog.add_meta(metadata.from_z2m_devices(payload))
            return
        self.catalog.add_meta(metadata.from_statestream(topic, self.statestream_base))

        for sample in parse(topic, payload):
            if self.catalog.observe(sample.signal, sample.value, sample.text, time_ns / 1e9):
                self.new_signals.append(sample.signal)
            # A retained message is a replay of an old value whose time is
            # unknown; storing it with the receive time would falsify history.
            if retained:
                continue
            resolved = self.registry.resolve(sample.signal)
            if not resolved.store:
                continue
            tags = {"source": topic.split("/", 1)[0], **self.registry.tags(sample.signal)}
            self.writer.write(Point(sample.signal, tags, sample.value, sample.text, time_ns))
            self.stored += 1


class MqttRunner:
    """Background MQTT connection feeding `handler(topic, payload, retained)`."""

    def __init__(self, handler, host: str, port: int, subscribe: list[str], username: str | None = None,
                 password: str | None = None):
        import paho.mqtt.client as mqtt

        self.host, self.port, self.subscribe = host, port, subscribe
        self.connected = False
        self.messages = 0
        self.last_message: float | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hpn-observer-{int(time.time())}")
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._handler = handler

    def _on_connect(self, cl, userdata, flags, reason, props):
        self.connected = not reason.is_failure
        log.info("MQTT connected to %s:%s (%s)", self.host, self.port, reason)
        for pattern in self.subscribe:
            cl.subscribe(pattern, qos=0)

    def _on_disconnect(self, cl, userdata, flags, reason, props):
        self.connected = False
        log.warning("MQTT disconnected (%s)", reason)

    def _on_message(self, cl, userdata, msg):
        self.messages += 1
        self.last_message = time.time()
        try:
            self._handler(msg.topic, msg.payload, msg.retain)
        except Exception:  # one bad message must not stop the ingest
            log.exception("message on %s failed", msg.topic)

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def run_mqtt(ingest: Ingest, host: str, port: int, subscribe: list[str], username: str | None = None,
             password: str | None = None, duration_s: float | None = None, on_tick=None) -> None:
    """Feed the ingest until duration_s passes (None: forever)."""
    runner = MqttRunner(ingest.handle, host, port, subscribe, username, password)
    runner.start()
    started = time.time()
    try:
        while duration_s is None or time.time() - started < duration_s:
            time.sleep(1)
            if on_tick:
                on_tick()
    finally:
        runner.stop()
