"""The simulator plays Home Assistant on the controller's MQTT broker.

* circuit demands  -> <base>/cmd/heating_loop_1 | heating_loop_2 | floor_heating (ON/OFF)
* keep-alive       -> active demands are re-published every `keepalive_s`
                      (controller demand_timeout_s = 600 s); can be paused
* floor setpoint   -> <base>/cmd/floor_setpoint ("35.0")
* discovery        -> retained HA discovery configs for a virtual mixer
                      position sensor and a DHW-charging binary sensor; the
                      controller binds them when its mixer_meter_* /
                      dhw_meter_* settings name this device.

Without a broker (`--mqtt-broker` absent / mock mode) the same commands go
through the emulation REST endpoint /api/sim/intent instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("hil.mqtt")

CIRCUITS = ("heating_loop_1", "heating_loop_2", "floor_heating")


class DemandManager:
    def __init__(self, base_topic: str, broker: str | None = None, user: str | None = None,
                 password: str | None = None, device: str = "floor_sim", rest_client=None,
                 keepalive_s: float = 300.0):
        self.base = base_topic.rstrip("/")
        self.device = device
        self.rest = rest_client
        self.keepalive_s = keepalive_s
        self.keepalive_paused = False
        self.demands = {c: False for c in CIRCUITS}
        self.setpoint = 35.0
        self.last_keepalive = 0.0
        self.published = 0
        self.last_error = ""
        self.client = None
        self.connected = False
        self.broker = broker
        if broker:
            import paho.mqtt.client as mqtt

            host, _, port = broker.partition(":")
            self.host, self.port = host, int(port or 1883)
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=f"hil-sim-{int(time.time()) % 100000}")
            if user:
                self.client.username_pw_set(user, password or None)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.reconnect_delay_set(1, 30)

    # -- paho callbacks (network thread) ---------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = not reason_code.is_failure
        log.info("MQTT connected to %s:%s (%s)", self.host, self.port, reason_code)
        if self.connected:
            self._publish_discovery()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected (%s)", reason_code)

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if self.client is not None:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()

    def stop(self):
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    @property
    def transport(self) -> str:
        return "mqtt" if self.client is not None else "rest"

    # -- topics ------------------------------------------------------------
    @property
    def mixer_state_topic(self):
        return f"{self.device}/sensor/mixer_position/state"

    @property
    def dhw_state_topic(self):
        return f"{self.device}/binary_sensor/dhw_charging/state"

    def _publish(self, topic, payload, retain=False):
        if self.client is None:
            return
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != 0:
            self.last_error = f"publish {topic} rc={info.rc}"
        else:
            self.published += 1

    def _publish_discovery(self):
        dev = {"identifiers": [self.device], "name": "Floor HIL simulator", "manufacturer": "HeatpumpsN"}
        self._publish(f"homeassistant/sensor/{self.device}/mixer_position/config", json.dumps({
            "name": "Mixer position", "unique_id": f"{self.device}_mixer_position",
            "state_topic": self.mixer_state_topic, "unit_of_measurement": "%",
            "device": dev}), retain=True)
        self._publish(f"homeassistant/binary_sensor/{self.device}/dhw_charging/config", json.dumps({
            "name": "DHW charging", "unique_id": f"{self.device}_dhw_charging",
            "state_topic": self.dhw_state_topic, "payload_on": "ON", "payload_off": "OFF",
            "device": dev}), retain=True)

    # -- commands ----------------------------------------------------------
    async def set_demand(self, circuit: str, on: bool):
        if circuit not in self.demands:
            raise ValueError(f"unknown circuit {circuit}")
        self.demands[circuit] = bool(on)
        await self._send_demands([circuit])

    async def set_setpoint(self, value: float):
        value = round(min(max(float(value), 5.0), 80.0), 1)
        self.setpoint = value
        if self.client is not None:
            self._publish(f"{self.base}/cmd/floor_setpoint", f"{value:.1f}")
        elif self.rest is not None:
            # the firmware's /api/sim/intent ignores this field (MQTT is its only
            # setpoint path); the mock controller honours it
            try:
                await self.rest.post("/api/sim/intent", {"floor_setpoint": value})
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)

    async def _send_demands(self, circuits):
        if self.client is not None:
            for c in circuits:
                self._publish(f"{self.base}/cmd/{c}", "ON" if self.demands[c] else "OFF")
        elif self.rest is not None:
            try:
                await self.rest.post("/api/sim/intent", {c: self.demands[c] for c in circuits})
                self.published += 1
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("intent failed: %s", e)

    async def keepalive_tick(self):
        if self.keepalive_paused:
            return
        now = time.time()
        if now - self.last_keepalive < self.keepalive_s:
            return
        self.last_keepalive = now
        active = [c for c, on in self.demands.items() if on]
        if active:
            await self._send_demands(active)

    async def publish_sensors(self, mixer_pos_pct: float | None, dhw: bool):
        if self.client is None:
            return
        if mixer_pos_pct is not None:
            self._publish(self.mixer_state_topic, f"{mixer_pos_pct:.1f}")
        self._publish(self.dhw_state_topic, "ON" if dhw else "OFF")

    async def all_off(self):
        for c in CIRCUITS:
            self.demands[c] = False
        await self._send_demands(list(CIRCUITS))
        if self.client is not None:
            await asyncio.sleep(0.5)  # let the network thread flush QoS 1 publishes

    def status(self) -> dict:
        return dict(transport=self.transport, broker=self.broker, connected=self.connected,
                    base_topic=self.base, demands=dict(self.demands), setpoint=self.setpoint,
                    keepalive_paused=self.keepalive_paused, keepalive_s=self.keepalive_s,
                    next_keepalive_s=max(self.keepalive_s - (time.time() - self.last_keepalive), 0.0),
                    published=self.published, last_error=self.last_error)
