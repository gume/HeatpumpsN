"""REST bridge to the ESP32 controller (emulation firmware, -DHPN_IO_EMULATION).

* `fetch_state()`  - /api/state + /api/sim, flattened into `Telemetry`.
* `Injector`       - pushes the three temperature roles to /api/sim/temperature,
                     slew-limited so the controller's L1 gradient cutoff never trips.
* `preflight()`    - verifies the emulation build before any injection.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("hil.controller")

ROLES = ("boiler", "return", "mix")
EMU_T_MIN, EMU_T_MAX = 15.0, 85.0


class PreflightError(RuntimeError):
    pass


@dataclass
class Telemetry:
    ok: bool = False
    ts: float = 0.0
    error: str = ""
    # relays (physical)
    pump_floor: bool = False
    pump_rad1: bool = False
    pump_rad2: bool = False
    heater: bool = False
    # demand as seen by the controller
    demand_floor: bool = False
    demand_rad1: bool = False
    demand_rad2: bool = False
    demand_age: dict = field(default_factory=dict)
    # mixer
    mix_motion: bool = False
    mix_dir: int = 0
    pos_est_dr: float = 0.0        # controller dead reckoning [%]
    mix_target: float = 0.0        # controller target [%]
    pos_phys: float | None = None  # emulated carrier position [%]
    mixer_integrator: float = 0.0
    # temperatures as the controller reads them
    t_boiler: float | None = None
    t_mix: float | None = None
    t_return: float | None = None
    # system
    error_code: int = 0
    error_text: str = ""
    safety_flags: list = field(default_factory=list)
    dhw_active: bool = False
    power: dict = field(default_factory=dict)
    mqtt_connected: bool = False
    uptime: int = 0
    mpc: dict = field(default_factory=dict)     # predictive controller status (fw >= 0.4)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def describe(e: Exception) -> str:
    """httpx timeouts stringify to ''; always name the exception type."""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _b(v) -> bool:
    return bool(v) and v != 0


def parse_state(st: dict, sim: dict | None) -> Telemetry:
    c = st.get("circuits", {})
    fl, r1, r2 = c.get("floor", {}), c.get("rad1", {}), c.get("rad2", {})
    temps = st.get("temps", {})
    sysd = st.get("system", {})
    safety = st.get("safety", {})
    t = Telemetry(ok=True, ts=time.time())
    t.pump_floor, t.pump_rad1, t.pump_rad2 = _b(fl.get("phys")), _b(r1.get("phys")), _b(r2.get("phys"))
    t.heater = _b(st.get("heater", {}).get("phys"))
    t.demand_floor, t.demand_rad1, t.demand_rad2 = (_b(fl.get("intended")), _b(r1.get("intended")),
                                                     _b(r2.get("intended")))
    t.demand_age = dict(floor=fl.get("demandAge", 0), rad1=r1.get("demandAge", 0), rad2=r2.get("demandAge", 0))
    t.mix_motion = _b(fl.get("mixerMotion"))
    t.mix_dir = int(fl.get("mixerDir") or 0)
    t.pos_est_dr = float(fl.get("mixerPos") or 0.0)
    t.mix_target = float(fl.get("mixerTarget") or 0.0)
    t.mixer_integrator = float(sysd.get("mixerIntegrator") or 0.0)
    t.t_boiler = temps.get("boilerSupply")
    t.t_mix = temps.get("mixerForward")
    t.t_return = temps.get("return")
    t.error_code = int(safety.get("errorCode") or sysd.get("errorCode") or 0)
    t.error_text = safety.get("errorText") or sysd.get("errorText") or ""
    t.safety_flags = list(safety.get("flags") or [])
    t.dhw_active = _b(sysd.get("dhwActive"))
    pw = st.get("power", {})
    t.power = {k: float(pw.get(k) or 0.0) for k in ("floor", "rad1", "rad2", "mixer")}
    t.mqtt_connected = _b(st.get("mqtt", {}).get("connected"))
    t.uptime = int(st.get("uptime") or 0)
    t.mpc = dict(st.get("mpc") or {})
    if sim:
        relays = sim.get("relays", {})
        # the emulated carrier's effective outputs are the physical truth in HIL
        t.pump_floor = _b(relays.get("floor", t.pump_floor))
        t.pump_rad1 = _b(relays.get("rad1", t.pump_rad1))
        t.pump_rad2 = _b(relays.get("rad2", t.pump_rad2))
        t.heater = _b(relays.get("heater", t.heater))
        pos = sim.get("mixer", {}).get("position")
        if pos is not None:
            t.pos_phys = float(pos) * 100.0
    return t


class HpnAuth(httpx.Auth):
    """HPN1 challenge-response signing (firmware/docs/security.md) for httpx.

    Device nonces are single use; every authenticated response carries the next
    one in X-HPN-Nonce, so a steady poller needs no extra challenge requests.
    """

    requires_request_body = True
    requires_response_body = True

    def __init__(self, user: str, password: str):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "firmware" / "tools"))
        import hpn_auth  # noqa: PLC0415
        self._h = hpn_auth
        self.user, self.password = user, password
        self.k_auth: bytes | None = None
        self.nonces: list[str] = []

    def _challenge_request(self, request: httpx.Request) -> httpx.Request:
        return httpx.Request("GET", request.url.copy_with(path="/api/auth/challenge", query=f"user={self.user}".encode()))

    def auth_flow(self, request: httpx.Request):
        for attempt in range(2):
            if self.k_auth is None or not self.nonces:
                resp = yield self._challenge_request(request)
                c = resp.json()
                if self.k_auth is None:
                    self.k_auth, _ = self._h.derive_keys(self.password, c["salt"], c["iter"])
                self.nonces.append(c["nonce"])
            nonce = self.nonces.pop(0)
            body_hash = hashlib.sha256(request.content or b"").hexdigest()
            sig = self._h.signature(self.k_auth, request.method, str(request.url.raw_path, "ascii"), nonce, body_hash)
            request.headers["X-HPN-Auth"] = f"user={self.user},nonce={nonce},sig={sig}"
            response = yield request
            if response.headers.get("X-HPN-Nonce"):
                self.nonces.append(response.headers["X-HPN-Nonce"])
            if response.status_code == 401 and response.headers.get("X-HPN-Reason") == "nonce" and attempt == 0:
                self.nonces.clear()
                continue
            return


class ControllerClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 3.0, auth: tuple[str, str] | None = None):
        # Firmware >= 0.4 requires HPN1-signed requests on every API route.
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport,
                                      auth=HpnAuth(*auth) if auth else None)
        self.base_url = base_url

    async def close(self):
        await self.http.aclose()

    async def get(self, path: str) -> dict:
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, body: dict) -> dict:
        r = await self.http.post(path, json=body)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"{path}: {r.status_code} {r.text[:200]}", request=r.request, response=r)
        return r.json()

    async def fetch_state(self) -> Telemetry:
        try:
            st = await self.get("/api/state")
        except Exception as e:  # noqa: BLE001
            return Telemetry(ok=False, ts=time.time(), error=f"/api/state: {describe(e)}")
        sim = None
        try:
            sim = await self.get("/api/sim")
        except Exception as e:  # noqa: BLE001
            log.debug("/api/sim failed: %s", describe(e))
        return parse_state(st, sim)

    async def preflight(self) -> dict:
        """Fail unless the controller runs an emulation build (spec section 7, P1)."""
        try:
            status = await self.get("/api/status")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise PreflightError("controller rejected the portal login: check --controller-user/"
                                     "--controller-password (or HPN_PORTAL_PASSWORD)") from e
            raise PreflightError(f"controller error at {self.base_url}: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"controller unreachable at {self.base_url}: {describe(e)}") from e
        emulation = bool(status.get("portal", {}).get("emulation"))
        try:
            sim = await self.get("/api/sim")
        except Exception:  # noqa: BLE001
            sim = {}
        if not emulation or not sim.get("enabled"):
            raise PreflightError(
                "controller is not running an emulation build (/api/sim unavailable). Flash the "
                "esp32dev_sim environment (-DHPN_IO_EMULATION): "
                "cd firmware && pio remote run -e esp32dev_sim --target upload")
        # the sine-wave boiler and manual carrier overrides would fight the plant
        await self.post("/api/sim/mode", {"followController": True, "realisticPower": True,
                                          "realisticBoiler": False})
        try:
            await self.post("/api/sim/overrides/clear", {})
        except Exception:  # noqa: BLE001
            pass
        info = dict(version=status.get("version"), build=status.get("build"),
                    hostname=status.get("hostname"), ramp=bool(sim.get("rampSupported")),
                    uptime=status.get("uptime"), reset_reason=status.get("resetReason"))
        try:
            # The full export is sealed (it holds credentials); the tuning
            # lives in the hardware section, which carries no secrets.
            hw = await self.get("/api/config/hardware")
            tuning = {k: hw.get(k) for k in ("kp", "ki", "delay_s", "mix_ff_offset", "ff_boiler",
                                             "ff_return", "rad_coupling_ff", "travel_time_ms") if k in hw}
            info["tuning"] = tuning
            info["tuning_sha256"] = hashlib.sha256(json.dumps(tuning, sort_keys=True).encode()).hexdigest()
        except Exception as e:  # noqa: BLE001
            log.warning("controller tuning unavailable: %s", describe(e))
        try:
            net = await self.get("/api/config/network")
            info["mqtt_base_topic"] = net.get("mqtt_base_topic")
            info["mqtt_host"] = net.get("mqtt_host")
            info["mqtt_port"] = net.get("mqtt_port")
        except Exception:  # noqa: BLE001
            pass
        return info


class Injector:
    """Slew-limited temperature injection.

    Every 10 s plant tick sets new targets; `tick()` (1 s) moves each injected
    value towards its target linearly over one plant period, never faster than
    `max_rate` K/s, and posts it when it changed (or every `refresh_s`).
    """

    def __init__(self, client: ControllerClient, max_rate: float = 0.15, period: float = 10.0,
                 refresh_s: float = 10.0, ramp: bool = False):
        self.client = client
        # ramp: the firmware interpolates (fw >= 0.4): one POST per role and plant
        # tick, no aliasing against the controller's own 1 s temperature poll
        self.ramp = ramp
        self.max_rate = max_rate
        self.period = period
        self.refresh_s = refresh_s
        self.target: dict[str, float] = {}
        self.value: dict[str, float] = {}
        self.rate: dict[str, float] = {}
        self.last_post: dict[str, tuple[float, float]] = {}
        self.errors = 0
        self.last_error = ""

    def set_targets(self, targets: dict[str, float]):
        for role, v in targets.items():
            v = min(max(float(v), EMU_T_MIN), EMU_T_MAX)
            self.target[role] = v
            cur = self.value.setdefault(role, v)
            self.rate[role] = min(abs(v - cur) / self.period, self.max_rate)

    def seed(self, current: dict[str, float]):
        """Start from what the controller currently reads (avoids a jump)."""
        for role, v in current.items():
            if v is not None:
                self.value[role] = min(max(float(v), EMU_T_MIN), EMU_T_MAX)

    async def push_ramps(self):
        """Ramp mode: post each role's next point; the firmware ramps over one period."""
        for role in ROLES:
            if role not in self.target:
                continue
            cur, tgt = self.value[role], self.target[role]
            span = self.max_rate * self.period
            nxt = min(max(tgt, cur - span), cur + span)
            try:
                await self.client.post("/api/sim/temperature", {"role": role, "value": round(nxt, 3),
                                                                "valid": True, "ramp_s": self.period})
                self.value[role] = nxt
                self.last_post[role] = (nxt, time.time())
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = describe(e)
                log.warning("injection %s failed: %s", role, describe(e))

    async def tick(self, dt: float = 1.0):
        if self.ramp:
            return
        now = time.time()
        for role in ROLES:
            if role not in self.target:
                continue
            cur, tgt = self.value[role], self.target[role]
            step = self.rate[role] * dt
            if abs(tgt - cur) <= step or step == 0.0:
                cur = tgt
            else:
                cur += step if tgt > cur else -step
            self.value[role] = cur
            last = self.last_post.get(role)
            if last is None or abs(last[0] - cur) >= 0.01 or now - last[1] >= self.refresh_s:
                try:
                    await self.client.post("/api/sim/temperature",
                                           {"role": role, "value": round(cur, 3), "valid": True})
                    self.last_post[role] = (cur, now)
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self.last_error = describe(e)
                    log.warning("injection %s failed: %s", role, describe(e))

    def status(self) -> dict:
        return dict(value=dict(self.value), target=dict(self.target), errors=self.errors,
                    last_error=self.last_error, max_rate=self.max_rate, ramp=self.ramp)
