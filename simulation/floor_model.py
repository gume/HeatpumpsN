"""Floor Heating Plant Physics Model

Implements thermodynamic and hydraulic ODEs for:
1. Boiler supply and secondary heat exchanger (sinusoid modulation / PID regulation)
2. Mixer valve travel, nonlinear blending curve x(pos), transport delay, and thermal sensor lag
3. Floor loop hydraulics (pipe travel delay) and floor slab thermal inertia
"""
import math
import json
import os
from collections import deque

DEFAULT_PARAMS_FILE = os.path.join(os.path.dirname(__file__), "identified_parameters.json")

class FloorHeatingPlant:
    def __init__(self, params=None, params_file=DEFAULT_PARAMS_FILE):
        if params is not None:
            self.p = params
        elif os.path.exists(params_file):
            with open(params_file, "r") as f:
                self.p = json.load(f)
        else:
            # Fallback default identified values
            self.p = {
                "boiler": {
                    "nominal_temp_c": 67.9,
                    "incoming_nominal_c": 65.0,
                    "oscillation_period_s": 3600.0,
                    "oscillation_amplitude_k": 3.5,
                    "warmup_rate_k_per_s": 0.05,
                    "cooldown_rate_k_per_s": -0.047
                },
                "mixer": {
                    "dead_time_s": 120.0,
                    "tau_mix_s": 30.0,
                    "travel_time_ms": 145000,
                    "valve_curve_c1": 1.1354,
                    "valve_curve_c2": -1.0844
                },
                "floor": {
                    "loop_delay_s": 30.0,
                    "nominal_return_temp_c": 25.2,
                    "steady_state_delta_t_k": 8.81,
                    "min_useful_delta_t_k": 2.0,
                    "tau_floor_s": 2400.0,
                    "return_rise_rate_k_per_s": 0.005,
                    "cooldown_rate_k_per_s": -0.002
                }
            }

        # Ambient / baseline cold temperature
        self.t_ambient = 20.0

        # State variables
        self.t_boiler = self.p["boiler"]["nominal_temp_c"]
        self.t_incoming = self.p["boiler"]["incoming_nominal_c"]
        self.t_return = self.p["floor"]["nominal_return_temp_c"]
        self.t_mix_forward = self.t_return + 5.0 # initial mixed temp
        self.mixer_pos = 0.50 # initial 50% position
        self.delta_t = self.t_mix_forward - self.t_return

        # Slab thermal state
        self.t_slab = self.t_return
        self.active_delta_t = self.p["floor"]["steady_state_delta_t_k"]

        self.elapsed_time_s = 0.0

        # Delay queues: (timestamp, value)
        self.mixer_delay_queue = deque()
        self.floor_delay_queue = deque()

    def reset(self, initial_pos=0.0, cold_start=False):
        """Reset plant to initial state."""
        self.elapsed_time_s = 0.0
        self.mixer_pos = max(0.0, min(1.0, initial_pos))
        
        if cold_start:
            self.t_boiler = 30.0
            self.t_incoming = 28.0
            self.t_return = 20.0
            self.t_mix_forward = 20.0
            self.t_slab = 20.0
            self.active_delta_t = 12.0
        else:
            self.t_boiler = self.p["boiler"]["nominal_temp_c"]
            self.t_incoming = self.p["boiler"]["incoming_nominal_c"]
            self.t_return = self.p["floor"]["nominal_return_temp_c"]
            self.t_mix_forward = self.t_return + (self.t_incoming - self.t_return) * self._valve_fraction(self.mixer_pos)
            self.t_slab = self.t_return
            self.active_delta_t = self.p["floor"]["steady_state_delta_t_k"]

        self.delta_t = self.t_mix_forward - self.t_return
        self.mixer_delay_queue.clear()
        self.floor_delay_queue.clear()

    def _valve_fraction(self, pos):
        """Evaluate nonlinear valve blending characteristic x(pos)."""
        u = max(0.0, min(1.0, pos))
        if u <= 0.001:
            return 0.0
        if u >= 0.999:
            return 1.0
        c1 = self.p["mixer"]["valve_curve_c1"]
        c2 = self.p["mixer"]["valve_curve_c2"]
        x = c1 * u + c2 * (u ** 2)
        return max(0.0, min(1.0, x))

    def step(self, dt, heater_relay, floor_pump_relay, mixer_motion, mixer_open):
        """Advance plant simulation by dt seconds.

        Args:
            dt: Timestep in seconds.
            heater_relay: bool, whether boiler heating relay is ON.
            floor_pump_relay: bool, whether floor circulation pump is ON.
            mixer_motion: bool, whether mixer motor is energized.
            mixer_open: bool, True=opening, False=closing.

        Returns:
            dict of updated plant telemetry.
        """
        self.elapsed_time_s += dt

        # ---------------------------------------------------------------------
        # 1. Update Mixer Mechanical Position
        # ---------------------------------------------------------------------
        if mixer_motion:
            travel_s = self.p["mixer"]["travel_time_ms"] / 1000.0
            step_pos = (dt / travel_s) * (1.0 if mixer_open else -1.0)
            self.mixer_pos = max(0.0, min(1.0, self.mixer_pos + step_pos))

        # ---------------------------------------------------------------------
        # 2. Update Boiler & Secondary Heat Exchanger
        # ---------------------------------------------------------------------
        b_p = self.p["boiler"]
        if heater_relay:
            # Sinusoidal modulation around nominal setpoint (PID steady cycle)
            w = 2.0 * math.pi / b_p["oscillation_period_s"]
            target_boiler = b_p["nominal_temp_c"] + b_p["oscillation_amplitude_k"] * math.sin(w * self.elapsed_time_s)
            
            # Warm up or follow modulation
            if self.t_boiler < target_boiler:
                self.t_boiler = min(target_boiler, self.t_boiler + b_p["warmup_rate_k_per_s"] * dt)
            else:
                self.t_boiler = max(target_boiler, self.t_boiler + b_p["cooldown_rate_k_per_s"] * dt)
        else:
            # Boiler idle cooldown towards ambient
            if self.t_boiler > self.t_ambient:
                cooling_scale = (self.t_boiler - self.t_ambient) / (b_p["nominal_temp_c"] - self.t_ambient)
                self.t_boiler = max(self.t_ambient, self.t_boiler + b_p["cooldown_rate_k_per_s"] * cooling_scale * dt)

        # Secondary incoming temperature follows boiler through heat exchanger
        hx_drop = max(0.0, b_p["nominal_temp_c"] - b_p["incoming_nominal_c"])
        self.t_incoming = max(self.t_ambient, self.t_boiler - hx_drop)

        # ---------------------------------------------------------------------
        # 3. Update Mixer Blending & Forward Temperature
        # ---------------------------------------------------------------------
        x_hot = self._valve_fraction(self.mixer_pos)
        t_blend_ideal = x_hot * self.t_incoming + (1.0 - x_hot) * self.t_return

        # Transport dead time L_mix via queue
        dead_time_s = self.p["mixer"]["dead_time_s"]
        self.mixer_delay_queue.append((self.elapsed_time_s, t_blend_ideal))

        # Retrieve value from dead_time_s ago
        t_blend_delayed = t_blend_ideal
        while self.mixer_delay_queue:
            t_sample, val = self.mixer_delay_queue[0]
            if self.elapsed_time_s - t_sample >= dead_time_s:
                t_blend_delayed = val
                self.mixer_delay_queue.popleft()
            else:
                break

        # Mixing chamber & sensor first-order lag (tau_mix)
        tau_mix = max(1.0, self.p["mixer"]["tau_mix_s"])
        alpha_mix = dt / (tau_mix + dt)
        self.t_mix_forward += alpha_mix * (t_blend_delayed - self.t_mix_forward)

        # ---------------------------------------------------------------------
        # 4. Update Floor Hydronic Loop & Return Temperature
        # ---------------------------------------------------------------------
        f_p = self.p["floor"]
        if floor_pump_relay:
            # Water is actively circulating through floor pipes
            loop_delay_s = f_p["loop_delay_s"]
            self.floor_delay_queue.append((self.elapsed_time_s, self.t_mix_forward))

            t_fwd_delayed = self.t_mix_forward
            while self.floor_delay_queue:
                t_sample, val = self.floor_delay_queue[0]
                if self.elapsed_time_s - t_sample >= loop_delay_s:
                    t_fwd_delayed = val
                    self.floor_delay_queue.popleft()
                else:
                    break

            # Dynamic floor delta-T: slab gradually warms up, delta_T approaches steady-state
            tau_floor = f_p["tau_floor_s"]
            alpha_floor = dt / (tau_floor + dt)
            self.active_delta_t += alpha_floor * (f_p["steady_state_delta_t_k"] - self.active_delta_t)

            # Return temperature tracks forward temperature minus floor heat dissipation
            t_ret_target = t_fwd_delayed - self.active_delta_t
            # Return sensor response
            self.t_return += alpha_floor * (t_ret_target - self.t_return)
        else:
            # Pump OFF: no circulation, standing water in manifold slowly cools
            cool_rate = f_p["cooldown_rate_k_per_s"]
            if self.t_return > self.t_ambient:
                self.t_return = max(self.t_ambient, self.t_return + cool_rate * dt)
            if self.t_mix_forward > self.t_return:
                self.t_mix_forward = max(self.t_return, self.t_mix_forward + cool_rate * 2.0 * dt)

        self.delta_t = self.t_mix_forward - self.t_return

        return {
            "elapsed_s": round(self.elapsed_time_s, 2),
            "t_boiler": round(self.t_boiler, 2),
            "t_incoming": round(self.t_incoming, 2),
            "t_mix_forward": round(self.t_mix_forward, 2),
            "t_return": round(self.t_return, 2),
            "delta_t": round(self.delta_t, 2),
            "mixer_pos": round(self.mixer_pos, 4),
            "x_hot": round(x_hot, 4),
            "heater_on": heater_relay,
            "floor_pump_on": floor_pump_relay,
            "mixer_motion": mixer_motion,
            "mixer_open": mixer_open,
        }
