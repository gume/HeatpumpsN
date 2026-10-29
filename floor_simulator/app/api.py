"""FastAPI app: dashboard (static), JSON control API and a WebSocket feed for the UI."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .boiler import MODES
from .engine import PRESETS, HilEngine
from .ha_mqtt import CIRCUITS

STATIC = Path(__file__).parent / "static"


class CircuitBody(BaseModel):
    circuit: str
    on: bool


class SetpointBody(BaseModel):
    value: float = Field(ge=5.0, le=80.0)


class BoilerBody(BaseModel):
    mode: str
    static_temp: float | None = Field(default=None, ge=20.0, le=85.0)


class DhwBody(BaseModel):
    action: str                     # on | off | timed
    duration_s: float | None = Field(default=None, gt=0, le=4 * 3600)


class OutdoorBody(BaseModel):
    mean: float = Field(ge=-30, le=40)
    amplitude: float = Field(ge=0, le=20)
    peak_hour: float = Field(default=15.0, ge=0, le=24)


class PresetBody(BaseModel):
    name: str


class KeepaliveBody(BaseModel):
    paused: bool


def create_app(engine_factory, lifespan_hooks=None) -> FastAPI:
    """`engine_factory` is an async callable returning a started HilEngine."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = await engine_factory()
        try:
            yield
        finally:
            if lifespan_hooks and "shutdown" in lifespan_hooks:
                await lifespan_hooks["shutdown"](app.state.engine)

    app = FastAPI(title="Floor-heating HIL simulator", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def eng() -> HilEngine:
        return app.state.engine

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/snapshot")
    def snapshot():
        return eng().snapshot()

    @app.get("/api/history")
    def history(seconds: float = 3600):
        return eng().history_since(min(max(seconds, 10), 12 * 3600))

    @app.get("/api/events")
    def events():
        e = eng()
        return {"mixer": list(e.events), "relays": list(e.relay_events)}

    @app.get("/api/session")
    def session():
        e = eng()
        return {"session": e.logs.meta if e.logs else None, "preflight": e.preflight_info}

    @app.post("/api/control/circuit")
    async def circuit(body: CircuitBody):
        if body.circuit not in CIRCUITS:
            raise HTTPException(400, f"circuit must be one of {CIRCUITS}")
        await eng().set_demand(body.circuit, body.on)
        return eng().demands.status()

    @app.post("/api/control/setpoint")
    async def setpoint(body: SetpointBody):
        await eng().set_setpoint(body.value)
        return eng().demands.status()

    @app.post("/api/control/boiler")
    def boiler(body: BoilerBody):
        if body.mode not in MODES:
            raise HTTPException(400, f"mode must be one of {MODES}")
        eng().set_boiler(body.mode, body.static_temp)
        return eng().boiler.status()

    @app.post("/api/control/dhw")
    async def dhw(body: DhwBody):
        if body.action not in ("on", "off", "timed"):
            raise HTTPException(400, "action must be on, off or timed")
        await eng().set_dhw(body.action, body.duration_s)
        return eng().boiler.status()

    @app.post("/api/control/outdoor")
    def outdoor(body: OutdoorBody):
        eng().set_outdoor(body.mean, body.amplitude, body.peak_hour)
        return eng().snapshot(light=True)["outdoor"]

    @app.post("/api/control/preset")
    def preset(body: PresetBody):
        if body.name not in PRESETS:
            raise HTTPException(400, f"preset must be one of {list(PRESETS)}")
        eng().apply_preset(body.name)
        return {"preset": body.name}

    @app.post("/api/control/keepalive")
    def keepalive(body: KeepaliveBody):
        eng().set_keepalive_paused(body.paused)
        return eng().demands.status()

    @app.post("/api/control/wear/reset")
    def wear_reset():
        eng().wear.reset()
        return eng().wear.status()

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        e = eng()
        e.listeners.add(q)
        try:
            await sock.send_json({"type": "snapshot", "snapshot": e.snapshot()})
            n = 0
            while True:
                msg = await q.get()
                await sock.send_json(msg)
                n += 1
                if n % 10 == 0:   # full snapshot (counters, events) every 10 s
                    await sock.send_json({"type": "snapshot", "snapshot": e.snapshot()})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            e.listeners.discard(q)

    return app
