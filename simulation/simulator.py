"""Hardware-in-the-Loop (HIL) & Plant Simulator

Simulates the home floor hydronic heating system in closed-loop with the ESP32 controller.
Communicates with the ESP32 emulator REST API:
- GET  /api/sim              -> Reads controller actuator outputs (relays, mixer motion/direction/position)
- POST /api/sim/temperature  -> Injects simulated physical temperatures (boiler, mix forward, floor return)
- POST /api/sim/intent       -> Injects simulated Home Assistant heat demand keepalive
- GET  /api/state            -> Reads controller state, alarms, and divergence diagnostics

Outputs raw time-series data to CSV for plotting with `simulation/plot_run.py` or any plotter.
Also supports `--mock` mode to run an in-memory L2/L1 controller clone if the physical ESP32 is offline.
"""
import sys
import os
import time
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime

from floor_model import FloorHeatingPlant

class ControllerRestClient:
    """HTTP client communicating with ESP32 board emulator REST API."""
    def __init__(self, base_url="http://localhost:9090", timeout=3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_sim_state(self):
        url = f"{self.base_url}/api/sim"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def set_temperature(self, role, value, valid=True):
        url = f"{self.base_url}/api/sim/temperature"
        data = json.dumps({"role": role, "value": float(value), "valid": bool(valid)}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_intent(self, heater=True, pump_floor=True, keepalive=True):
        url = f"{self.base_url}/api/sim/intent"
        payload = {
            "heater": bool(heater),
            "pump_floor": bool(pump_floor),
            "keepalive": bool(keepalive)
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_system_state(self):
        try:
            url = f"{self.base_url}/api/state"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {}


class MockController:
    """Software-in-the-Loop clone of the ESP32 L2 Coordinator & L1 Safety servo.

    Used when physical/remote ESP32 is offline to test plant reactions, oscillations,
    and reaction speeds directly.
    """
    def __init__(self, setpoint=35.0):
        self.setpoint = setpoint
        self.heater_relay = True
        self.floor_pump_relay = True
        self.pos_est_dr = 0.50
        self.mix_target_pos = 0.50
        self.mix_motion = False
        self.mix_open = False
        self.mix_dir = 0
        self.integrator = 0.0

        # Firmware L2 tuning parameters from report.md
        self.ff_boiler = 0.47
        self.ff_return = 0.87
        self.mix_ff_offset = 6.75
        self.kp = 0.04
        self.ki = 0.002
        self.delay_s = 120.0
        self.deadband_temp = 2.0
        self.deadband_pos = 0.02
        self.travel_time_ms = 145000

        self.last_fwd_temp = None
        self.gradient = 0.0

    def step(self, dt, t_boiler, t_incoming, t_fwd, t_ret):
        # 1. Gradient
        if self.last_fwd_temp is not None:
            self.gradient = (t_fwd - self.last_fwd_temp) / dt
        self.last_fwd_temp = t_fwd

        # 2. L1 Mixer Servo: update dead-reckoning position
        if self.mix_motion:
            step_dr = (dt / (self.travel_time_ms / 1000.0)) * (1.0 if self.mix_open else -1.0)
            self.pos_est_dr = max(0.0, min(1.0, self.pos_est_dr + step_dr))
            
            # Stop if reached target or limits
            if (self.mix_open and self.pos_est_dr >= self.mix_target_pos) or \
               (not self.mix_open and self.pos_est_dr <= self.mix_target_pos):
                self.mix_motion = False
                self.mix_dir = 0
            elif self.pos_est_dr <= 0.0 or self.pos_est_dr >= 1.0:
                self.mix_motion = False
                self.mix_dir = 0

        # 3. L2 Mixer Control Tick
        hot_span = self.ff_boiler * t_incoming - self.ff_return * t_ret
        pos_ff = 0.0
        if abs(hot_span) > 1.0:
            pos_ff = (self.setpoint - self.mix_ff_offset - self.ff_return * t_ret) / hot_span
        pos_ff = max(0.0, min(1.0, pos_ff))

        predicted = t_fwd + self.gradient * self.delay_s
        error = self.setpoint - predicted

        # Integrator
        if abs(error) > self.deadband_temp:
            self.integrator += self.ki * error
            self.integrator = max(-0.25, min(0.25, self.integrator))

        # In deadband: hold position (Fix 2.1 in firmware handover)
        if abs(error) <= self.deadband_temp:
            target = self.pos_est_dr
        else:
            target = pos_ff + self.kp * error + self.integrator

        # Clamping step to 0.10 per control tick
        max_step = 0.10
        if target > self.pos_est_dr + max_step:
            target = self.pos_est_dr + max_step
        elif target < self.pos_est_dr - max_step:
            target = self.pos_est_dr - max_step
        self.mix_target_pos = max(0.0, min(1.0, target))

        # L1 Actuation start
        if not self.mix_motion:
            err_pos = self.mix_target_pos - self.pos_est_dr
            if abs(err_pos) > self.deadband_pos:
                self.mix_motion = True
                self.mix_open = (err_pos > 0)
                self.mix_dir = 1 if self.mix_open else -1

        return {
            "relays": {"heater": self.heater_relay, "floor": self.floor_pump_relay},
            "mixer": {
                "motion": self.mix_motion,
                "open": self.mix_open,
                "position": self.pos_est_dr,
            },
            "controller": {
                "estimatedPosition": self.pos_est_dr * 100.0,
                "targetPosition": self.mix_target_pos * 100.0,
                "motion": self.mix_motion,
                "direction": self.mix_dir
            }
        }


def run_simulation(args):
    print("=" * 70)
    print(" HARDWARE-IN-THE-LOOP (HIL) FLOOR HEATING SIMULATOR")
    print("=" * 70)

    # Initialize Plant Model
    plant = FloorHeatingPlant()
    plant.reset(initial_pos=0.50, cold_start=args.cold)
    print(f"Plant model initialized (nominal boiler: {plant.t_boiler:.1f}°C, return: {plant.t_return:.1f}°C)")

    # Prepare Output CSV
    if not args.output:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_tag = "mock" if args.mock else "hil"
        args.output = os.path.join(os.path.dirname(__file__), f"sim_run_{mode_tag}_{ts_str}.csv")

    csv_file = open(args.output, "w")
    csv_header = (
        "elapsed_s,t_boiler,t_incoming,t_mix_fwd,t_return,delta_t,mixer_pos_plant,"
        "mixer_pos_dr,mixer_pos_target,mixer_motion,mixer_dir,relay_heater,relay_floor,"
        "error_code,divergence\n"
    )
    csv_file.write(csv_header)
    csv_file.flush()
    print(f"Logging raw simulation telemetry to:\n  {args.output}\n")

    client = None
    mock_ctrl = None
    if args.mock:
        print("[MODE] Running in MOCK (Software-in-the-Loop) mode.")
        mock_ctrl = MockController(setpoint=args.setpoint)
    else:
        print(f"[MODE] Running in HIL mode against ESP32 at: {args.url}")
        client = ControllerRestClient(base_url=args.url)
        # Probe connection
        try:
            print("Connecting to ESP32 emulator interface...")
            init_sim = client.get_sim_state()
            print(f"Connected to ESP32! Emulator revision: {init_sim.get('revision', 0)}")
            if args.intent:
                print("Sending initial heat demand intent...")
                client.send_intent(heater=True, pump_floor=True, keepalive=True)
        except Exception as e:
            print(f"\n[ERROR] Could not connect to ESP32 at {args.url}: {e}")
            print("\nHint: To test without physical hardware right now, run with --mock:")
            print(f"  python3 simulation/simulator.py --mock --duration-s {args.duration_s}\n")
            csv_file.close()
            return

    dt = args.step_s
    max_duration = args.duration_s if args.duration_s > 0 else float("inf")
    elapsed = 0.0

    last_intent_time = 0.0
    print("-" * 70)
    print("Elapsed | T_Boiler | T_Incoming | T_Mix (Fwd) | T_Return | Delta-T | Valve (Plant/DR/Tgt) | Actuation")
    print("-" * 70)

    try:
        while elapsed <= max_duration:
            step_start = time.time()

            # 1. Read Controller State
            if args.mock:
                ctrl_state = mock_ctrl.step(
                    dt,
                    plant.t_boiler,
                    plant.t_incoming,
                    plant.t_mix_forward,
                    plant.t_return,
                )
                error_code = "ERR_OK"
                divergence = "DIV_NONE"
            else:
                try:
                    ctrl_state = client.get_sim_state()
                    sys_st = client.get_system_state()
                    error_code = sys_st.get("system", {}).get("errorCode", "OK")
                    divergence = sys_st.get("system", {}).get("divergence", "NONE")
                except Exception as ex:
                    print(f"\n[WARN] REST read error at t={elapsed:.1f}s: {ex}")
                    time.sleep(dt)
                    continue

            relays = ctrl_state.get("relays", {})
            heater_on = relays.get("heater", True)
            floor_pump_on = relays.get("floor", True)

            mixer = ctrl_state.get("mixer", {})
            mixer_motion = mixer.get("motion", False)
            mixer_open = mixer.get("open", False)

            controller_info = ctrl_state.get("controller", {})
            pos_dr = controller_info.get("estimatedPosition", plant.mixer_pos * 100.0) / 100.0
            pos_target = controller_info.get("targetPosition", plant.mixer_pos * 100.0) / 100.0
            mix_dir = controller_info.get("direction", 1 if mixer_open else (-1 if mixer_motion else 0))

            # 2. Advance Plant Physics Model
            plant_data = plant.step(
                dt=dt,
                heater_relay=heater_on,
                floor_pump_relay=floor_pump_on,
                mixer_motion=mixer_motion,
                mixer_open=mixer_open,
            )

            # 3. Inject Simulated Temperatures into ESP32
            if not args.mock:
                try:
                    client.set_temperature("boiler", plant.t_boiler, valid=True)
                    client.set_temperature("mix", plant.t_mix_forward, valid=True)
                    client.set_temperature("return", plant.t_return, valid=True)

                    # Periodically refresh demand keepalive
                    if args.intent and (elapsed - last_intent_time >= 10.0):
                        client.send_intent(heater=True, pump_floor=True, keepalive=True)
                        last_intent_time = elapsed
                except Exception as ex:
                    print(f"\n[WARN] REST write error at t={elapsed:.1f}s: {ex}")

            # 4. Write CSV record
            row = (
                f"{elapsed:.2f},{plant.t_boiler:.2f},{plant.t_incoming:.2f},"
                f"{plant.t_mix_forward:.2f},{plant.t_return:.2f},{plant.delta_t:.2f},"
                f"{plant.mixer_pos:.4f},{pos_dr:.4f},{pos_target:.4f},"
                f"{1 if mixer_motion else 0},{mix_dir},"
                f"{1 if heater_on else 0},{1 if floor_pump_on else 0},"
                f"{error_code},{divergence}\n"
            )
            csv_file.write(row)
            if int(elapsed) % 5 == 0:
                csv_file.flush()

            # 5. Console status line
            act_str = f"Htr:{'ON' if heater_on else 'OFF'} Pmp:{'ON' if floor_pump_on else 'OFF'}"
            if mixer_motion:
                act_str += f" Mot:{'OPEN' if mixer_open else 'CLOSE'}"
            else:
                act_str += " Mot:IDLE"

            print(
                f"{elapsed:6.0f}s | "
                f"{plant.t_boiler:7.1f}°C | "
                f"{plant.t_incoming:9.1f}°C | "
                f"{plant.t_mix_forward:10.2f}°C | "
                f"{plant.t_return:7.2f}°C | "
                f"{plant.delta_t:6.2f}K | "
                f"{plant.mixer_pos*100:4.1f}% / {pos_dr*100:4.1f}% / {pos_target*100:4.1f}% | "
                f"{act_str}"
            )

            elapsed += dt

            # Timing pacing (if real-time)
            if not args.fast:
                time_taken = time.time() - step_start
                if time_taken < dt:
                    time.sleep(dt - time_taken)

    except KeyboardInterrupt:
        print("\nSimulation interrupted by user.")
    finally:
        csv_file.close()
        print("-" * 70)
        print(f"Simulation completed! Log saved to:\n  {args.output}")
        print(f"To plot results, run:\n  python3 simulation/plot_run.py {args.output}\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Floor Heating HIL Simulator")
    parser.add_argument("--url", default="http://localhost:9090", help="ESP32 controller portal base URL")
    parser.add_argument("--mock", action="store_true", help="Run with built-in mock controller (offline testing)")
    parser.add_argument("--step-s", type=float, default=1.0, help="Simulation timestep in seconds (default: 1.0)")
    parser.add_argument("--duration-s", type=float, default=600.0, help="Run duration in seconds (default: 600s, 0=infinite)")
    parser.add_argument("--setpoint", type=float, default=35.0, help="Floor forward temperature setpoint (default: 35.0 C)")
    parser.add_argument("--cold", action="store_true", help="Cold start (initial cold floor 20 C)")
    parser.add_argument("--intent", action="store_true", default=True, help="Auto-send simulated heat demand intent")
    parser.add_argument("--fast", action="store_true", help="Run at maximum speed (no wall-clock sleep)")
    parser.add_argument("--output", default=None, help="Output CSV filepath")
    return parser.parse_args()


if __name__ == "__main__":
    run_simulation(parse_args())
