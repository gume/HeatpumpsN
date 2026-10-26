"""Simulation Telemetry Plotter & Performance Evaluator

Reads CSV simulation logs output by `simulation/simulator.py` and produces:
1. High-resolution multi-panel plots (Temperatures, Valve Position, Actuator States)
2. Performance analysis metrics:
   - Rise time (to reach setpoint within deadband)
   - Settling time
   - Max overshoot / peak temperature
   - Oscillation frequency and amplitude
   - Motor run-time & pulse frequency (to verify whether motor is thrashing or stable)
"""
import sys
import os

# Auto-configure cached wheels for matplotlib & numpy if needed
WHEEL_PATHS = [
    "/home/gume/.cache/uv/archive-v0/RJxmlONr_gdY1xrq", # numpy 1.26
    "/home/gume/.cache/uv/archive-v0/AiIBKS2_gwTm-Lz3", # matplotlib
    "/home/gume/.cache/uv/archive-v0/uA6jWvJIV5wpk7Am", # dateutil
    "/home/gume/.cache/uv/archive-v0/XyPFJWQHJQE-iO19", # pyparsing
    "/home/gume/.cache/uv/archive-v0/MCVYeHNLkcq7Uw44", # packaging
    "/home/gume/.cache/uv/archive-v0/5xAXLwjrrFufqsvj", # contourpy
    "/home/gume/.cache/uv/archive-v0/vczjdcS4fD7iOFT3", # fontTools
    "/home/gume/.cache/uv/archive-v0/SJwz8CJhsRc-jFYK", # kiwisolver
    "/home/gume/.cache/uv/archive-v0/DDbg0a80kJD5JaNW", # cycler
]
for p in WHEEL_PATHS:
    if os.path.exists(p) and p not in sys.path:
        sys.path.insert(0, p)

os.environ["MPLCONFIGDIR"] = "/tmp"

import csv
import numpy as np

def load_csv(csv_path):
    data = {
        "elapsed_s": [],
        "t_boiler": [],
        "t_incoming": [],
        "t_mix_fwd": [],
        "t_return": [],
        "delta_t": [],
        "mixer_pos_plant": [],
        "mixer_pos_dr": [],
        "mixer_pos_target": [],
        "mixer_motion": [],
        "mixer_dir": [],
        "relay_heater": [],
        "relay_floor": [],
        "error_code": [],
        "divergence": [],
    }
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data["elapsed_s"].append(float(row["elapsed_s"]))
            data["t_boiler"].append(float(row["t_boiler"]))
            data["t_incoming"].append(float(row["t_incoming"]))
            data["t_mix_fwd"].append(float(row["t_mix_fwd"]))
            data["t_return"].append(float(row["t_return"]))
            data["delta_t"].append(float(row["delta_t"]))
            data["mixer_pos_plant"].append(float(row["mixer_pos_plant"]))
            data["mixer_pos_dr"].append(float(row["mixer_pos_dr"]))
            data["mixer_pos_target"].append(float(row["mixer_pos_target"]))
            data["mixer_motion"].append(int(row["mixer_motion"]))
            data["mixer_dir"].append(int(row["mixer_dir"]))
            data["relay_heater"].append(int(row["relay_heater"]))
            data["relay_floor"].append(int(row["relay_floor"]))
            data["error_code"].append(row["error_code"])
            data["divergence"].append(row["divergence"])

    for k in data:
        if k not in ["error_code", "divergence"]:
            data[k] = np.array(data[k])
    return data


def compute_metrics(data, setpoint=35.0, deadband=2.0):
    t = data["elapsed_s"]
    fwd = data["t_mix_fwd"]
    motion = data["mixer_motion"]
    dt = np.median(np.diff(t)) if len(t) > 1 else 1.0

    # 1. Motor activity
    total_time = t[-1] - t[0] if len(t) > 1 else 1.0
    motor_active_s = float(np.sum(motion) * dt)
    motor_duty_pct = (motor_active_s / total_time) * 100.0

    # Number of motor start/stop events
    motor_starts = int(np.sum((motion[1:] == 1) & (motion[:-1] == 0)))

    # 2. Temperature metrics
    in_deadband = np.abs(fwd - setpoint) <= deadband
    
    # Rise time (first time entering deadband)
    rise_idx = np.where(in_deadband)[0]
    rise_time_s = float(t[rise_idx[0]]) if len(rise_idx) > 0 else None

    # Max overshoot
    max_fwd = float(np.max(fwd))
    overshoot_k = max(0.0, max_fwd - setpoint)

    # Steady state error (last 20% of run)
    tail_len = max(1, len(fwd) // 5)
    tail_fwd = fwd[-tail_len:]
    ss_mean = float(np.mean(tail_fwd))
    ss_error = ss_mean - setpoint
    ss_ripple = float(np.ptp(tail_fwd)) # peak to peak

    return {
        "duration_s": total_time,
        "rise_time_s": rise_time_s,
        "overshoot_k": overshoot_k,
        "max_temp_c": max_fwd,
        "ss_mean_temp_c": ss_mean,
        "ss_error_k": ss_error,
        "ss_ripple_k": ss_ripple,
        "motor_active_s": motor_active_s,
        "motor_duty_pct": motor_duty_pct,
        "motor_start_count": motor_starts,
    }


def plot_telemetry(csv_path, output_png=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"[WARN] Matplotlib not available ({e}). Skipping graphical plot.")
        return

    data = load_csv(csv_path)
    metrics = compute_metrics(data)

    if not output_png:
        output_png = os.path.splitext(csv_path)[0] + ".png"

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 10), sharex=True, gridspec_kw={'height_ratios': [2.5, 1.8, 1.2]})
    t_min = data["elapsed_s"] / 60.0 # display in minutes

    # Panel 1: Temperatures
    ax1.plot(t_min, data["t_boiler"], label=r"$T_{boiler}$ (Primary Supply)", color="#d9534f", linewidth=1.5, alpha=0.85)
    ax1.plot(t_min, data["t_incoming"], label=r"$T_{incoming}$ (Secondary Hot In)", color="#f0ad4e", linewidth=1.5, alpha=0.85)
    ax1.plot(t_min, data["t_mix_fwd"], label=r"$T_{forward}$ (Mixed Flow into Floor)", color="#5cb85c", linewidth=2.2)
    ax1.plot(t_min, data["t_return"], label=r"$T_{return}$ (Floor Return)", color="#0275d8", linewidth=2.0)

    # Setpoint & Deadband
    setpoint = 35.0
    deadband = 2.0
    ax1.axhline(setpoint, color="black", linestyle="--", linewidth=1.2, label=f"Setpoint ({setpoint}°C)")
    ax1.axhspan(setpoint - deadband, setpoint + deadband, color="gray", alpha=0.15, label=f"Deadband (±{deadband}K)")

    ax1.set_ylabel("Temperature (°C)", fontsize=11, fontweight="bold")
    ax1.set_title("Hardware-in-the-Loop Floor Heating Simulation — Thermal & Actuator Telemetry", fontsize=13, fontweight="bold")
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(loc="upper left", ncol=3, fontsize=9)

    # Secondary axis for Delta-T
    ax1_twin = ax1.twinx()
    ax1_twin.plot(t_min, data["delta_t"], color="#6f42c1", linestyle=":", linewidth=1.4, alpha=0.7, label=r"$\Delta T = T_{fwd} - T_{ret}$")
    ax1_twin.set_ylabel("Floor ΔT (K)", color="#6f42c1", fontsize=10)
    ax1_twin.tick_params(axis='y', labelcolor="#6f42c1")
    ax1_twin.set_ylim(0, 20)

    # Panel 2: Valve Position
    ax2.plot(t_min, data["mixer_pos_plant"] * 100.0, label="Plant True Valve Position", color="#292b2c", linewidth=2.0)
    ax2.plot(t_min, data["mixer_pos_dr"] * 100.0, label="Controller Dead Reckoning (DR)", color="#0275d8", linestyle="--", linewidth=1.8)
    ax2.plot(t_min, data["mixer_pos_target"] * 100.0, label="Controller Target Position", color="#d9534f", linestyle=":", linewidth=1.5)
    ax2.set_ylabel("Valve Position (%)", fontsize=11, fontweight="bold")
    ax2.set_ylim(-2, 102)
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(loc="upper right", fontsize=9)

    # Panel 3: Motor Motion & Relays
    ax3.step(t_min, data["mixer_dir"], where="post", label="Mixer Motor (+1 Open, -1 Close, 0 Idle)", color="#0275d8", linewidth=1.5)
    ax3.step(t_min, data["relay_floor"] * 0.5, where="post", label="Floor Pump Relay", color="#5cb85c", linewidth=1.2, linestyle="--")
    ax3.set_ylabel("Actuators", fontsize=11, fontweight="bold")
    ax3.set_xlabel("Elapsed Time (minutes)", fontsize=11, fontweight="bold")
    ax3.set_yticks([-1, 0, 0.5, 1])
    ax3.set_yticklabels(["CLOSE", "IDLE", "PUMP", "OPEN"])
    ax3.set_ylim(-1.3, 1.3)
    ax3.grid(True, linestyle=":", alpha=0.6)
    ax3.legend(loc="lower right", ncol=2, fontsize=9)

    plt.tight_layout()
    plt.savefig(output_png, dpi=160)
    plt.close()
    print(f"Graphical plot saved to:\n  {output_png}\n")


def print_summary(csv_path):
    data = load_csv(csv_path)
    m = compute_metrics(data)
    print("=" * 65)
    print(f" SIMULATION PERFORMANCE METRICS: {os.path.basename(csv_path)}")
    print("=" * 65)
    rise_str = f"{m['rise_time_s']:.1f} s" if m['rise_time_s'] is not None else "Not reached"
    print(f"Total Duration:              {m['duration_s']:.1f} s ({m['duration_s']/60:.1f} min)")
    print(f"Rise Time (to ±2K deadband): {rise_str}")
    print(f"Peak Forward Temperature:    {m['max_temp_c']:.2f} °C (Overshoot: +{m['overshoot_k']:.2f} K)")
    print(f"Steady-State Forward Temp:   {m['ss_mean_temp_c']:.2f} °C (Error: {m['ss_error_k']:+.2f} K)")
    print(f"Steady-State Ripple (p-to-p): {m['ss_ripple_k']:.2f} K")
    print("-" * 65)
    print(f"Mixer Motor Total Run Time:  {m['motor_active_s']:.1f} s ({m['motor_duty_pct']:.1f}% duty cycle)")
    print(f"Mixer Motor Start/Stops:     {m['motor_start_count']} movements")
    print("=" * 65)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 simulation/plot_run.py <path_to_simulation_csv>")
        sys.exit(1)

    csv_p = sys.argv[1]
    if not os.path.exists(csv_p):
        print(f"Error: CSV file not found: {csv_p}")
        sys.exit(1)

    print_summary(csv_p)
    plot_telemetry(csv_p)
