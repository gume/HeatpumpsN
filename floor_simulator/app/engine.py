"""HIL orchestrator: ties the plant, boiler, controller bridge, HA demands and logs together.

Loops
  * telemetry (1 s)  - poll /api/state + /api/sim, step the slew-limited injector,
                       detect relay / mixer actuation events, wear metrics, keep-alive;
  * physics  (10 s)  - boiler emulator + coupled plant step, new injection targets,
                       hil_timeseries row, virtual HA sensors;
  * slow     (30 s)  - /api/counters and the controller's setpoint shadow.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass

from .boiler import BoilerEmulator
from .controller import ControllerClient, Injector, Telemetry
from .ha_mqtt import CIRCUITS, DemandManager
from .logstore import LogStore
from .plant import DT, PlantEngine, PlantInputs, PlantParams

log = logging.getLogger("hil.engine")

HISTORY_S = 12 * 3600
RMSE_WINDOW_S = 3600
EVENT_LOG_MAX = 500

DHW_DURATIONS = {"median": 1170.0, "p90": 1626.0}

# plant states per preset: T_inc water, T_fwd, T_ret, T_slab, T_room, T_boiler (+ diverter)
PRESETS = {
    "cold_slab_start": dict(label="Cold Slab Start", t_inc=22.0, t_fwd=19.0, t_ret=19.0, t_slab=18.5,
                            t_room=18.0, t_boiler=22.0),
    "steady_winter": dict(label="Steady Winter", t_inc=60.0, t_fwd=33.0, t_ret=27.5, t_slab=26.0,
                          t_room=21.0, t_boiler=68.7),
    "hot_slab": dict(label="Hot Slab", t_inc=55.0, t_fwd=33.0, t_ret=31.0, t_slab=30.0,
                     t_room=23.0, t_boiler=65.0),
    "post_dhw_hot_slug": dict(label="Post-DHW Hot Slug", t_inc=76.0, t_fwd=30.0, t_ret=27.0, t_slab=26.0,
                              t_room=21.0, t_boiler=81.5, park_diverter=True),
}


@dataclass
class Outdoor:
    mean: float = 2.0
    amplitude: float = 4.0
    peak_hour: float = 15.0

    def at(self, ts: float) -> float:
        lt = time.localtime(ts)
        hour = lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0
        return self.mean + self.amplitude * math.cos(2 * math.pi * (hour - self.peak_hour) / 24.0)


class WearMetrics:
    def __init__(self):
        self.reset()

    def reset(self):
        self.started = time.time()
        self.travel = 0.0
        self.last_pos: float | None = None
        self.moves = 0
        self.reversals = 0
        self.last_dir = 0
        self.relay_cycles = {k: 0 for k in ("heater", "floor", "rad1", "rad2")}
        self.last_relays: dict | None = None
        self.err = collections.deque()   # (ts, error^2) while the floor pump runs

    def update(self, t: Telemetry, pos: float | None, setpoint: float):
        if pos is not None:
            if self.last_pos is not None:
                self.travel += abs(pos - self.last_pos)
            self.last_pos = pos
        relays = dict(heater=t.heater, floor=t.pump_floor, rad1=t.pump_rad1, rad2=t.pump_rad2)
        if self.last_relays is not None:
            for k, v in relays.items():
                if v and not self.last_relays[k]:
                    self.relay_cycles[k] += 1
        self.last_relays = relays
        if t.pump_floor and t.t_mix is not None:
            self.err.append((t.ts, (t.t_mix - setpoint) ** 2))
        while self.err and self.err[0][0] < t.ts - RMSE_WINDOW_S:
            self.err.popleft()

    def on_move(self, direction: int):
        self.moves += 1
        if self.last_dir and direction and direction != self.last_dir:
            self.reversals += 1
        if direction:
            self.last_dir = direction

    def status(self) -> dict:
        hours = max((time.time() - self.started) / 3600.0, 1e-6)
        rmse = math.sqrt(sum(e for _, e in self.err) / len(self.err)) if self.err else None
        return dict(total_travel_pct=self.travel, travel_rate_pct_h=self.travel / hours,
                    moves=self.moves, reversals=self.reversals, reversals_per_h=self.reversals / hours,
                    relay_cycles=dict(self.relay_cycles), tfwd_rmse_1h=rmse,
                    rmse_samples=len(self.err), since=self.started)


class HilEngine:
    def __init__(self, client: ControllerClient, demands: DemandManager, logstore: LogStore | None,
                 params: PlantParams | None = None, max_rate: float = 0.15, seed: int | None = None,
                 preset: str = "steady_winter"):
        self.params = params or PlantParams.load()
        self.client = client
        self.demands = demands
        self.logs = logstore
        self.plant = PlantEngine(self.params, act_mode=0)
        self.boiler = BoilerEmulator(self.params.boiler, seed=seed)
        self.injector = Injector(client, max_rate=max_rate, period=DT)
        self.outdoor = Outdoor()
        self.wear = WearMetrics()
        self.telemetry = Telemetry()
        self.counters: dict = {}
        self.controller_setpoint: float | None = None
        self.preflight_info: dict = {}
        self.plant_out = self.plant.outputs()
        self.boiler_out = None
        self.last_physics = 0.0
        self.history: collections.deque = collections.deque(maxlen=HISTORY_S)
        self.events: collections.deque = collections.deque(maxlen=EVENT_LOG_MAX)
        self.relay_events: collections.deque = collections.deque(maxlen=EVENT_LOG_MAX)
        self._move: dict | None = None
        self._prev_t: Telemetry | None = None
        self._dhw_sent: bool | None = None
        self._tasks: list[asyncio.Task] = []
        self.listeners: set[asyncio.Queue] = set()
        self.apply_preset(preset)

    # ------------------------------------------------------------------ control
    def apply_preset(self, name: str):
        p = PRESETS[name]
        self.plant.set_state(t_inc=p["t_inc"], t_fwd=p["t_fwd"], t_ret=p["t_ret"], t_slab=p["t_slab"],
                             t_room=p["t_room"], park_diverter=p.get("park_diverter", False))
        self.boiler.tb = p["t_boiler"]
        self.plant_out = self.plant.outputs()
        self.preset = name
        self._push_targets()
        log.info("preset %s applied", name)

    async def set_demand(self, circuit: str, on: bool):
        await self.demands.set_demand(circuit, on)

    async def set_setpoint(self, value: float):
        await self.demands.set_setpoint(value)

    def set_boiler(self, mode: str, static_temp: float | None = None):
        self.boiler.set_mode(mode, static_temp)

    async def set_dhw(self, action: str, duration_s: float | None = None):
        if action == "off":
            self.boiler.dhw_stop()
        elif action == "on":
            self.boiler.dhw_start(None)
        elif action == "timed":
            self.boiler.dhw_start(duration_s or DHW_DURATIONS["median"])
        else:
            raise ValueError("action must be on, off or timed")
        await self._sync_dhw()

    def set_outdoor(self, mean: float, amplitude: float, peak_hour: float):
        self.outdoor = Outdoor(float(mean), float(amplitude), float(peak_hour) % 24.0)

    def set_keepalive_paused(self, paused: bool):
        self.demands.keepalive_paused = bool(paused)
        if not paused:
            self.demands.last_keepalive = 0.0   # refresh right away on resume

    # ------------------------------------------------------------------ loops
    async def start(self):
        t = await self.client.fetch_state()
        if t.ok:
            self.telemetry = t
            self.injector.seed({"boiler": t.t_boiler, "return": t.t_return, "mix": t.t_mix})
        self._push_targets()
        await self.demands.set_setpoint(self.demands.setpoint)
        self._tasks = [asyncio.create_task(self._telemetry_loop(), name="telemetry"),
                       asyncio.create_task(self._physics_loop(), name="physics"),
                       asyncio.create_task(self._slow_loop(), name="slow")]

    async def stop(self, safe: bool = True):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if safe:
            try:
                self.boiler.dhw_stop()
                await self._sync_dhw()
                await self.demands.all_off()
                log.info("safe shutdown: all demands OFF, DHW OFF")
            except Exception as e:  # noqa: BLE001
                log.warning("safe shutdown incomplete: %s", e)
        if self.logs:
            self.logs.close()

    async def _every(self, period: float, fn):
        nxt = time.monotonic()
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("loop %s failed", fn.__name__)
            nxt += period
            await asyncio.sleep(max(nxt - time.monotonic(), 0.0))

    async def _telemetry_loop(self):
        await self._every(1.0, self._telemetry_tick)

    async def _physics_loop(self):
        await self._every(DT, self._physics_tick)

    async def _slow_loop(self):
        await self._every(30.0, self._slow_tick)

    # ------------------------------------------------------------------ ticks
    def _valve_pos(self, t: Telemetry) -> float:
        return t.pos_phys if t.pos_phys is not None else t.pos_est_dr

    async def _telemetry_tick(self):
        t = await self.client.fetch_state()
        if t.ok:
            self._detect_events(t)
            self.wear.update(t, self._valve_pos(t), self.demands.setpoint)
            self.telemetry = t
        else:
            self.telemetry.ok = False
            self.telemetry.error = t.error
        await self.injector.tick(1.0)
        await self.demands.keepalive_tick()
        row = self._history_row()
        self.history.append(row)
        self._broadcast({"type": "tick", "row": row, "snapshot": self.snapshot(light=True)})

    async def _physics_tick(self):
        t = self.telemetry
        now = time.time()
        tout = self.outdoor.at(now)
        b = self.boiler.step(heat_call=t.heater, t_amb=self.plant.tro + self.params.hx[18])
        self.boiler_out = b
        pos = self._valve_pos(t)
        self.plant_out = self.plant.step(PlantInputs(tb=b.tb, bp=b.pump_pct, pmod=b.burner_pct, ww=b.dhw,
                                                     p1=t.pump_rad1, p2=t.pump_rad2, p3=t.pump_floor,
                                                     pos=pos, tout=tout))
        self.last_physics = now
        self._push_targets()
        await self._sync_dhw()
        await self.demands.publish_sensors(pos, b.dhw)
        if self.logs:
            o = self.plant_out
            self.logs.timeseries(dict(
                timestamp=now, t_boiler_primary=b.tb, t_inc_water=o["t_inc_water"],
                t_inc_sensor=o["t_inc_sensor"], t_fwd_mix=o["t_fwd_mix"], t_fwd_sensor=o["t_fwd_sensor"],
                t_fwd_setpoint=self.demands.setpoint, t_ret=o["t_ret"], t_slab=o["t_slab"],
                t_room=o["t_room"], t_out=tout, p1=t.pump_rad1, p2=t.pump_rad2, p3=t.pump_floor,
                dhw_active=b.dhw, burner_pct=b.burner_pct, mixer_state_pct=pos,
                mixer_integrator=t.mixer_integrator, system_error_code=t.error_code))

    async def _slow_tick(self):
        try:
            self.counters = await self.client.get("/api/counters")
        except Exception as e:  # noqa: BLE001
            log.debug("counters: %s", e)
        try:
            rt = await self.client.get("/api/config/runtime")
            self.controller_setpoint = rt.get("ha", {}).get("floor_setpoint_shadow")
        except Exception as e:  # noqa: BLE001
            log.debug("runtime: %s", e)

    def _push_targets(self):
        o = self.plant_out
        self.injector.set_targets({"boiler": o["t_inc_sensor"], "return": o["t_ret"], "mix": o["t_fwd_sensor"]})

    async def _sync_dhw(self):
        dhw = self.boiler.dhw_on and self.boiler.mode != "off"
        if dhw != self._dhw_sent:
            try:
                await self.client.post("/api/sim/intent", {"dhw_active": dhw})
                self._dhw_sent = dhw
            except Exception as e:  # noqa: BLE001
                log.warning("dhw intent failed: %s", e)

    # ------------------------------------------------------------------ events
    def _detect_events(self, t: Telemetry):
        prev = self._prev_t
        pos = self._valve_pos(t)
        if prev is not None:
            for name in ("heater", "pump_floor", "pump_rad1", "pump_rad2"):
                a, b = getattr(prev, name), getattr(t, name)
                if a != b:
                    self.relay_events.append(dict(ts=t.ts, relay=name, on=b))
            if t.error_code != prev.error_code:
                self.relay_events.append(dict(ts=t.ts, relay="error", on=bool(t.error_code),
                                              code=t.error_code, text=t.error_text))
            if t.mix_motion and not prev.mix_motion:
                self._move = dict(timestamp_start=t.ts, direction=t.mix_dir,
                                  pos_before_pct=self._valve_pos(prev))
                self.wear.on_move(t.mix_dir)
            elif t.mix_motion and self._move is not None and t.mix_dir != self._move["direction"]:
                # direction flipped without a stop: close this move and start another
                self._finish_move(t, pos)
                self._move = dict(timestamp_start=t.ts, direction=t.mix_dir, pos_before_pct=pos)
                self.wear.on_move(t.mix_dir)
            elif not t.mix_motion and prev.mix_motion and self._move is not None:
                self._finish_move(t, pos)
        self._prev_t = t

    def _finish_move(self, t: Telemetry, pos: float):
        m = self._move
        self._move = None
        ev = dict(m, timestamp_end=t.ts, duration_s=t.ts - m["timestamp_start"],
                  direction="open" if m["direction"] > 0 else "close" if m["direction"] < 0 else "none",
                  pos_after_pct=pos, delta_pos_pct=pos - m["pos_before_pct"], t_fwd=t.t_mix,
                  setpoint=self.demands.setpoint, p1_active=t.pump_rad1, p2_active=t.pump_rad2)
        self.events.append(ev)
        if self.logs:
            self.logs.event(ev)
        self._broadcast({"type": "mixer_event", "event": ev})

    # ------------------------------------------------------------------ views
    def _history_row(self) -> dict:
        t, o, b = self.telemetry, self.plant_out, self.boiler_out
        inj = self.injector.value
        return dict(
            ts=round(time.time(), 1),
            t_boiler=round(b.tb if b else self.boiler.tb, 3),
            t_inc_water=round(o["t_inc_water"], 3), t_inc_sensor=round(o["t_inc_sensor"], 3),
            t_fwd_sensor=round(o["t_fwd_sensor"], 3), t_ret=round(o["t_ret"], 3),
            t_slab=round(o["t_slab"], 3), t_room=round(o["t_room"], 3),
            t_out=round(self.outdoor.at(time.time()), 3),
            setpoint=self.demands.setpoint,
            inj_boiler=inj.get("boiler"), inj_return=inj.get("return"), inj_mix=inj.get("mix"),
            ctl_mix=t.t_mix, pos_phys=t.pos_phys, pos_est=t.pos_est_dr, pos_target=t.mix_target,
            heater=int(t.heater), floor=int(t.pump_floor), rad1=int(t.pump_rad1), rad2=int(t.pump_rad2),
            dhw=int(bool(b and b.dhw)), burner=b.burner_pct if b else 0.0, mix_motion=int(t.mix_motion),
            pw_floor=t.power.get("floor"), pw_rad1=t.power.get("rad1"), pw_rad2=t.power.get("rad2"),
            pw_mixer=t.power.get("mixer"), err=t.error_code, ok=int(t.ok))

    def history_since(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        return [r for r in self.history if r["ts"] >= cutoff]

    def snapshot(self, light: bool = False) -> dict:
        snap = dict(
            ts=time.time(), telemetry=self.telemetry.as_dict(), plant=self.plant_out,
            boiler=self.boiler.status(), boiler_out=self.boiler_out.__dict__ if self.boiler_out else None,
            injector=self.injector.status(), demands=self.demands.status(), wear=self.wear.status(),
            outdoor=dict(mean=self.outdoor.mean, amplitude=self.outdoor.amplitude,
                         peak_hour=self.outdoor.peak_hour, now=self.outdoor.at(time.time())),
            preset=self.preset, controller_setpoint=self.controller_setpoint,
            last_physics=self.last_physics, controller_url=self.client.base_url)
        if not light:
            snap.update(counters=self.counters, events=list(self.events)[-50:],
                        relay_events=list(self.relay_events)[-50:], preflight=self.preflight_info,
                        presets={k: v["label"] for k, v in PRESETS.items()}, circuits=list(CIRCUITS),
                        session=self.logs.meta if self.logs else None)
        return snap

    # ------------------------------------------------------------------ UI fan-out
    def _broadcast(self, msg: dict):
        for q in list(self.listeners):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
