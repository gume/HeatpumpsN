"""Offline contract tests: FastAPI routes, injection slew limit, demands, events (mock ESP32)."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from floor_simulator.app.api import create_app
from floor_simulator.app.boiler import BoilerEmulator
from floor_simulator.app.controller import ControllerClient, Injector, parse_state
from floor_simulator.app.engine import HilEngine
from floor_simulator.app.ha_mqtt import DemandManager
from floor_simulator.app.logstore import LogStore
from floor_simulator.app.mock_controller import MockController, create_mock_app
from floor_simulator.app.plant import PlantParams


def make_engine(tmp_path, mock=None):
    mock = mock or MockController()
    client = ControllerClient("http://mock", transport=httpx.ASGITransport(app=create_mock_app(mock)))
    demands = DemandManager("heatpumpsn/controller", rest_client=client)
    logs = LogStore(tmp_path, {"test": True})
    return HilEngine(client, demands, logs, seed=1), mock


@pytest.fixture
def app_client(tmp_path):
    async def factory():
        eng, _ = make_engine(tmp_path)
        eng.preflight_info = await eng.client.preflight()
        await eng.start()
        return eng

    async def shutdown(eng):
        await eng.stop()

    with TestClient(create_app(factory, {"shutdown": shutdown})) as c:
        yield c


def test_routes(app_client):
    c = app_client
    assert c.get("/").status_code == 200
    snap = c.get("/api/snapshot").json()
    assert snap["preset"] == "steady_winter" and "plant" in snap
    r = c.post("/api/control/circuit", json={"circuit": "floor_heating", "on": True}).json()
    assert r["demands"]["floor_heating"] is True
    assert c.post("/api/control/circuit", json={"circuit": "nope", "on": True}).status_code == 400
    assert c.post("/api/control/setpoint", json={"value": 32.5}).json()["setpoint"] == 32.5
    assert c.post("/api/control/setpoint", json={"value": 99}).status_code == 422
    assert c.post("/api/control/boiler", json={"mode": "static", "static_temp": 60}).json()["mode"] == "static"
    assert c.post("/api/control/boiler", json={"mode": "bogus"}).status_code == 400
    assert c.post("/api/control/dhw", json={"action": "timed", "duration_s": 120}).json()["dhw_on"] is True
    assert c.post("/api/control/dhw", json={"action": "off"}).json()["dhw_on"] is False
    assert c.post("/api/control/preset", json={"name": "hot_slab"}).json()["preset"] == "hot_slab"
    assert c.post("/api/control/keepalive", json={"paused": True}).json()["keepalive_paused"] is True
    assert c.post("/api/control/outdoor", json={"mean": -5, "amplitude": 3, "peak_hour": 14}).json()["mean"] == -5
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_injector_slew_limit():
    posted = []

    class Fake:
        async def post(self, path, body):
            posted.append(body)

    inj = Injector(Fake(), max_rate=0.15)
    inj.seed({"mix": 30.0})
    inj.set_targets({"mix": 50.0})          # a 20 K jump
    values = []
    for _ in range(60):
        asyncio.run(inj.tick(1.0))
        values.append(inj.value["mix"])
    steps = [b - a for a, b in zip([30.0] + values, values)]
    assert max(steps) <= 0.15 + 1e-9
    assert values[-1] == pytest.approx(30.0 + 60 * 0.15)
    inj.set_targets({"mix": 99.0})          # clamped to the emulation range
    assert inj.target["mix"] == 85.0


def test_injector_interpolates_small_steps():
    class Fake:
        async def post(self, path, body):
            pass

    inj = Injector(Fake(), max_rate=0.15)
    inj.seed({"mix": 30.0})
    inj.set_targets({"mix": 31.0})          # 1 K over one 10 s tick -> 0.1 K/s
    for _ in range(5):
        asyncio.run(inj.tick(1.0))
    assert inj.value["mix"] == pytest.approx(30.5)


def test_closed_loop_mock(tmp_path):
    async def run():
        eng, mock = make_engine(tmp_path)
        await eng.client.preflight()
        await eng._telemetry_tick()        # baseline sample with everything off
        await eng.set_demand("floor_heating", True)
        for k in range(30):                 # 30 s of telemetry, three plant ticks
            await eng._telemetry_tick()
            if k % 10 == 0:
                await eng._physics_tick()
        assert eng.telemetry.pump_floor and eng.telemetry.heater
        assert mock.grad_trips == 0
        assert eng.wear.relay_cycles["floor"] == 1
        await eng.stop()
        assert not mock.intent["floor_heating"]          # safe shutdown
    asyncio.run(run())
    files = list(tmp_path.glob("*/hil_timeseries_*.jsonl"))
    assert files and files[0].read_text().count("\n") == 3


def test_preflight_rejects_hardware_build():
    app = create_mock_app()
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/api/sim"]

    @app.get("/api/status", include_in_schema=False)
    def status():
        return {"portal": {"emulation": False}}

    client = ControllerClient("http://mock", transport=httpx.ASGITransport(app=app))
    from floor_simulator.app.controller import PreflightError
    with pytest.raises(PreflightError):
        asyncio.run(client.preflight())


def test_boiler_modes():
    b = BoilerEmulator(PlantParams.load().boiler, seed=3)
    b.set_mode("static", 60.0)
    assert b.step(True, 20.0).tb == 60.0
    b.set_mode("off")
    o = b.step(True, 20.0)
    assert o.burner_pct == 0.0 and o.tb < 60.0
    b.set_mode("observed")
    b.dhw_start(30)
    assert b.step(False, 20.0).dhw
    for _ in range(3):
        b.step(False, 20.0)
    assert not b.dhw_on


def test_parse_state_prefers_carrier():
    t = parse_state({"circuits": {"floor": {"phys": 0, "mixerPos": 10}}},
                    {"relays": {"floor": True}, "mixer": {"position": 0.5}})
    assert t.pump_floor and t.pos_phys == 50.0 and t.pos_est_dr == 10.0
