"""ESP32 floor-heating HIL simulator - headless service + web bench.

    uv run --project floor_simulator python -m floor_simulator.hil_server \\
        --controller-ip <ESP32_IP> --mqtt-broker <HOST:PORT> [--port 8080]

`--mock` runs against an in-process mock controller (no ESP32, no broker).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx
import uvicorn

from .app.api import create_app
from .app.controller import ControllerClient, PreflightError
from .app.engine import PRESETS, HilEngine
from .app.ha_mqtt import DemandManager
from .app.logstore import LogStore
from .app.plant import PARAMS_PATH, PlantParams

HERE = Path(__file__).resolve().parent
log = logging.getLogger("hil")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--controller-ip", help="ESP32 address, host[:port] or http://... URL")
    ap.add_argument("--controller-user", default=os.environ.get("HPN_PORTAL_USER", "admin"),
                    help="portal login (default admin, or $HPN_PORTAL_USER)")
    ap.add_argument("--controller-password", default=os.environ.get("HPN_PORTAL_PASSWORD", "admin"),
                    help="portal password (default admin, or $HPN_PORTAL_PASSWORD)")
    ap.add_argument("--mqtt-broker", help="HOST:PORT of the controller's broker; without it "
                    "demands go through /api/sim/intent and the setpoint cannot be set")
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-password")
    ap.add_argument("--mqtt-base", help="controller base topic (default: read from the controller)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--log-dir", type=Path, default=HERE / "logs")
    ap.add_argument("--mock", action="store_true", help="use the in-process mock controller")
    ap.add_argument("--preset", default="steady_winter", choices=list(PRESETS))
    ap.add_argument("--max-rate", type=float, default=0.15, help="injection slew limit [K/s]")
    ap.add_argument("--seed", type=int, help="boiler emulator RNG seed")
    ap.add_argument("--expect-tuning-hash", help="fail unless the controller's tuning hash matches")
    ap.add_argument("--no-safe-shutdown", action="store_true",
                    help="leave demands as they are on exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if not args.mock and not args.controller_ip:
        ap.error("--controller-ip is required (or use --mock)")
    return args


def controller_url(addr: str) -> str:
    return addr if addr.startswith(("http://", "https://")) else f"http://{addr}"


def build(args):
    params = PlantParams.load()
    transport = None
    url = "http://mock"
    if args.mock:
        from .app.mock_controller import create_mock_app

        transport = httpx.ASGITransport(app=create_mock_app())
    else:
        url = controller_url(args.controller_ip)
    client = ControllerClient(url, transport=transport,
                              auth=None if args.mock else (args.controller_user, args.controller_password))

    async def factory():
        try:
            info = await client.preflight()
        except PreflightError as e:
            log.error("PREFLIGHT FAILED: %s", e)
            raise SystemExit(2) from e
        log.info("controller %s: firmware %s (%s), tuning %s", url, info.get("version"),
                 info.get("build"), info.get("tuning"))
        if args.expect_tuning_hash and info.get("tuning_sha256") != args.expect_tuning_hash:
            log.error("tuning hash mismatch: controller %s != expected %s",
                      info.get("tuning_sha256"), args.expect_tuning_hash)
            raise SystemExit(3)
        base = args.mqtt_base or info.get("mqtt_base_topic") or "heatpumpsn/controller"
        demands = DemandManager(base, broker=None if args.mock else args.mqtt_broker,
                                user=args.mqtt_user, password=args.mqtt_password, rest_client=client)
        demands.start()
        meta = dict(controller=url, mock=args.mock, mqtt_broker=args.mqtt_broker, mqtt_base=base,
                    plant_parameters=str(PARAMS_PATH), plant_parameters_sha256=params.sha256,
                    max_rate=args.max_rate, seed=args.seed, preset=args.preset, controller_info=info)
        logs = LogStore(args.log_dir, meta)
        engine = HilEngine(client, demands, logs, params=params, max_rate=args.max_rate,
                           seed=args.seed, preset=args.preset)
        engine.preflight_info = info
        engine.injector.ramp = bool(info.get("ramp"))
        log.info("temperature injection: %s", "firmware ramps (1 POST/role/10 s)" if engine.injector.ramp
                 else "1 s steps (firmware without ramp support)")
        await engine.start()
        log.info("session %s, logs in %s", logs.session_id, logs.dir)
        return engine

    async def shutdown(engine: HilEngine):
        await engine.stop(safe=not args.no_safe_shutdown)
        engine.demands.stop()
        await client.close()

    return create_app(factory, {"shutdown": shutdown})


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build(args)
    # uvicorn handles SIGINT/SIGTERM and runs the lifespan shutdown (safe stop + log flush)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
