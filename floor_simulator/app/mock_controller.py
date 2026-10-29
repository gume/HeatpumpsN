"""Offline stand-in for the ESP32 emulation firmware (`--mock`, tests).

Implements the subset of the REST contract the simulator uses, with a crude
controller: a demanded circuit runs its pump, any demand calls the heater, and
the mixer is a deadband P controller on the injected `mix` temperature that
moves at 100 % / 120 s. Demands time out after 600 s unless refreshed.
It is a contract double, not a model of the real L2 control.
"""
from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request

TRAVEL_S = 120.0
DEMAND_TIMEOUT_S = 600.0


class MockController:
    def __init__(self):
        self.temps = {"boiler": 45.0, "return": 30.0, "mix": 35.0}
        self.intent = {"heating_loop_1": False, "heating_loop_2": False, "floor_heating": False}
        self.intent_ts = {k: time.time() for k in self.intent}
        self.dhw = False
        self.setpoint = 35.0
        self.pos = 0.0          # 0..1
        self.motion = False
        self.dir = 0
        self.last = time.time()
        self.boot = time.time()
        self.cycles = {k: 0 for k in ("heater", "rad1", "rad2", "floor", "mix_open", "mix_close")}
        self._relays = {"heater": False, "rad1": False, "rad2": False, "floor": False}
        self.revision = 0
        self.grad_trips = 0
        self._last_mix = (time.time(), 35.0)
        self.error_code = 0

    def _demand(self, key):
        return self.intent[key] and time.time() - self.intent_ts[key] < DEMAND_TIMEOUT_S

    def advance(self):
        now = time.time()
        dt = now - self.last
        self.last = now
        if self.motion:
            self.pos = min(max(self.pos + self.dir * dt / TRAVEL_S, 0.0), 1.0)
        relays = {"floor": self._demand("floor_heating"), "rad1": self._demand("heating_loop_1"),
                  "rad2": self._demand("heating_loop_2")}
        relays["heater"] = any(relays.values())
        for k, v in relays.items():
            if v and not self._relays[k]:
                self.cycles[k] += 1
        self._relays = relays
        if relays["floor"]:
            e = self.setpoint - self.temps["mix"]
            lo, hi = 0.20, 0.85
            if e > 1.0 and self.pos < hi:
                self.motion, self.dir = True, 1
            elif e < -1.0 and self.pos > lo:
                self.motion, self.dir = True, -1
            elif abs(e) < 0.3:
                self.motion, self.dir = False, 0
        else:
            self.motion, self.dir = (self.pos > 0.0), (-1 if self.pos > 0.0 else 0)

    def state(self):
        self.advance()
        demand_age = {k: int(time.time() - self.intent_ts[k]) for k in self.intent}
        circ = lambda key, relay: dict(intended=int(self._demand(key)), demand=int(self._demand(key)),  # noqa: E731
                                       cmd=int(self._relays[relay]), phys=int(self._relays[relay]),
                                       demandAge=demand_age[key], afterRun=300)
        floor = circ("floor_heating", "floor")
        floor.update(mixerPos=round(self.pos * 100, 1), mixerTarget=0, mixerMotion=self.motion,
                     mixerDir=self.dir)
        return {
            "uptime": int(time.time() - self.boot),
            "mqtt": {"connected": False},
            "circuits": {"rad1": circ("heating_loop_1", "rad1"), "rad2": circ("heating_loop_2", "rad2"),
                         "floor": floor},
            "temps": {"boilerSupply": self.temps["boiler"], "mixerForward": self.temps["mix"],
                      "return": self.temps["return"]},
            "heater": {"phys": int(self._relays["heater"])},
            "safety": {"flags": [], "errorCode": self.error_code,
                       "errorText": "No error" if not self.error_code else "gradient cutoff"},
            "system": {"errorCode": self.error_code, "dhwActive": self.dhw, "mixerIntegrator": 0},
            "power": {"floor": 10.0 * self._relays["floor"], "rad1": 10.0 * self._relays["rad1"],
                      "rad2": 10.0 * self._relays["rad2"], "mixer": 2.0 * self.motion},
        }


def create_mock_app(mock: MockController | None = None) -> FastAPI:
    m = mock or MockController()
    app = FastAPI(title="Mock ESP32 controller")
    app.state.mock = m

    @app.get("/api/status")
    def status():
        return {"ok": True, "version": "mock", "build": "mock", "hostname": "mock",
                "portal": {"emulation": True}}

    @app.get("/api/state")
    def state():
        return m.state()

    @app.get("/api/sim")
    def sim():
        m.advance()
        return {"enabled": True, "revision": m.revision,
                "temperatures": [{"role": k, "value": v, "valid": True} for k, v in m.temps.items()],
                "relays": dict(m._relays), "mixer": {"motion": m.motion, "open": m.dir > 0, "position": m.pos}}

    @app.post("/api/sim/temperature")
    async def temperature(req: Request):
        body = await req.json()
        role, value = body.get("role"), body.get("value")
        if role not in m.temps:
            raise HTTPException(400, "unknown temperature role")
        if not isinstance(value, (int, float)) or not 15.0 <= value <= 85.0:
            raise HTTPException(400, "temperature out of range (15-85 C)")
        if role == "mix":
            t0, v0 = m._last_mix
            now = time.time()
            if now - t0 > 0.5 and abs(value - v0) / (now - t0) > 0.3:
                m.grad_trips += 1
                m.error_code = 102
            m._last_mix = (now, value)
        m.temps[role] = float(value)
        m.revision += 1
        return {"ok": True, "role": role, "value": value}

    @app.post("/api/sim/intent")
    async def intent(req: Request):
        body = await req.json()
        for k in m.intent:
            if isinstance(body.get(k), bool):
                m.intent[k] = body[k]
                m.intent_ts[k] = time.time()
        if isinstance(body.get("floor_setpoint"), (int, float)):
            m.setpoint = float(body["floor_setpoint"])  # mock-only field
        if isinstance(body.get("dhw_active"), bool):
            m.dhw = body["dhw_active"]
        if body.get("keepalive"):
            for k in m.intent_ts:
                m.intent_ts[k] = time.time()
        return {"ok": True, **m.intent, "dhw_active": m.dhw}

    @app.post("/api/sim/mode")
    async def mode(req: Request):
        return {"ok": True, **(await req.json())}

    @app.post("/api/sim/overrides/clear")
    def clear():
        return {"ok": True}

    @app.get("/api/counters")
    def counters():
        return {"counters": [{"id": k, "cycles": v} for k, v in m.cycles.items()]}

    @app.get("/api/config/runtime")
    def runtime():
        return {"ha": {"floor_setpoint_shadow": m.setpoint}}

    @app.get("/api/config/network")
    def network():
        return {"mqtt_base_topic": "heatpumpsn/controller", "mqtt_host": "", "mqtt_port": 1883}

    @app.get("/api/config/export")
    def export():
        return {"format": "heatpumpsn.config", "hardware": {"kp": 0.5, "ki": 0.02, "travel_time_ms": 120000}}

    return app
