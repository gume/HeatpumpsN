"""Complete System Identification Suite

Runs identification on all three subsystems:
1. Boiler / Heater supply dynamics (sine oscillations & PID regulation)
2. Mixer valve blending & dynamics (dead time, time constant, characteristic)
3. Floor loop hydraulics & thermal mass (circulation delay, Delta-T, thermal inertia)

Saves results to `simulation/identified_parameters.json`.
"""
import sys
import os
import json

for p in [
    "/home/gume/.cache/uv/archive-v0/WGeC7MajuAJLhVwQ",
    "/home/gume/.cache/uv/archive-v0/cbT0G5uL0TvrpNBv",
]:
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

from sysid_heater import analyze_heater
from sysid_mixer import analyze_mixer
from sysid_floor import analyze_floor

OUTPUT_JSON = "/home/gume/HeatpumpsN/simulation/identified_parameters.json"

def main():
    print("=" * 65)
    print(" SYSTEM IDENTIFICATION: HEAT PUMP & FLOOR HEATING PLANT")
    print("=" * 65)

    print("\n>>> 1. Identifying Boiler / Heater Dynamics...")
    heater_params = analyze_heater()

    print("\n>>> 2. Identifying Mixer Blending & Valve Characteristic...")
    mixer_params = analyze_mixer()

    print("\n>>> 3. Identifying Floor Hydraulic & Thermal Loop Dynamics...")
    floor_params = analyze_floor()

    full_params = {
        "boiler": {
            "nominal_temp_c": round(heater_params["boiler_nominal_temp_c"], 2),
            "incoming_nominal_c": round(heater_params["incoming_nominal_c"], 2),
            "oscillation_period_s": round(heater_params["oscillation_period_s"], 1),
            "oscillation_amplitude_k": round(heater_params["oscillation_amplitude_k"], 2),
            "warmup_rate_k_per_s": round(heater_params["warmup_rate_k_per_s"], 4),
            "cooldown_rate_k_per_s": round(heater_params["cooldown_rate_k_per_s"], 4),
        },
        "mixer": {
            "dead_time_s": round(mixer_params["dead_time_s"], 1),
            "tau_mix_s": round(mixer_params["tau_mix_s"], 1),
            "travel_time_ms": 145000,
            "valve_curve_c1": round(mixer_params["valve_curve_c1"], 4),
            "valve_curve_c2": round(mixer_params["valve_curve_c2"], 4),
        },
        "floor": {
            "loop_delay_s": round(max(30.0, floor_params["loop_delay_s"]), 1),
            "nominal_return_temp_c": round(floor_params["nominal_return_temp_c"], 2),
            "steady_state_delta_t_k": round(floor_params["median_delta_t_k"], 2),
            "min_useful_delta_t_k": round(floor_params["min_useful_delta_t_k"], 1),
            "tau_floor_s": round(floor_params["tau_floor_s"], 1),
            "return_rise_rate_k_per_s": round(floor_params["return_rise_rate_k_per_s"], 5),
            "cooldown_rate_k_per_s": -0.002,
        }
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(full_params, f, indent=2)

    print("\n" + "=" * 65)
    print(f"IDENTIFICATION COMPLETE! Saved parameters to:")
    print(f"  {OUTPUT_JSON}")
    print("=" * 65)
    print(json.dumps(full_params, indent=2))

if __name__ == "__main__":
    main()
