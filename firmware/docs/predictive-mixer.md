# Predictive mixer control

From firmware 0.4.0 the floor mixer is driven by a model-predictive controller
(MPC) with a supervised fallback. The previous PI + feedforward loop is still in
the firmware as the `legacy` mode.

| File | Role |
|---|---|
| `include/hpn/mpc_core.h`, `src/mpc_core.cpp` | Controller, supervisor and fallback. Plain C++, no Arduino: the same code runs in the host bench |
| `include/hpn/mpc.h`, `src/mpc.cpp` | Firmware glue: NVS, locking, homing, MQTT/REST, telemetry |
| `src/l2_control.cpp` | Calls the glue on the floor-demand path |
| `floor_simulator/tools/fw_mpc.py` | ctypes binding of `mpc_core.cpp` for the offline closed loop |

## 1. Modes

```
          observer / HA command                       anomaly (supervisor)
 legacy  <───────────────────>  mpc  ──────────────────────────────>  homing
                                 ▲                                       │ full close stroke,
                                 │ 30 min stable, ≥ 60 min since anomaly │ dead reckoning := 0
                                 └──────────────  cautious  <────────────┘
```

* **mpc**: predictive control (section 2). Default after a fresh flash.
* **legacy**: the 0.3 loop (`kp`, `ki`, `delay_s`, feedforward).
* **homing**: a free-run close for `travel_time_ms + 10 s`. Afterwards the valve
  position is known to be 0 again.
* **cautious**: model-free fallback (section 3).

The preferred mode (`mpc` or `legacy`) is stored in NVS. A fallback is not
stored: after a reboot the controller starts in the preferred mode. Emergency
close, no-demand close and DHW handling still run before any of these modes.

## 2. Predictive controller

**Model.** It uses only what the controller measures, plus the identified
parameters:

```
T_inc_water ≈ T_boiler_sensor + tau_inc · d/dt(T_boiler_sensor)   (lead, filtered)
T_mix       = T_ret + x(pos) · (T_inc_water − T_ret)              x(): 11-knot law
T_fwd       = lag(tau_fwd) of T_mix delayed by delay_s
```

The model runs alongside the plant every 10 s. The filtered gap between
measured and modelled `T_fwd` is the **bias**. It is added to every prediction,
so a constant model error does not leave a steady offset. The bias only learns
while the supply is steady, because during a burner transient the error is a
timing error, not an offset.

**Plan.** Every `replan_s` seconds, or immediately when the measurement leaves
the last prediction by more than `trigger_k`, the controller does the following:

1. It enumerates 343 move sequences: one move at t0, at t0 + 60 s and at
   t0 + 180 s, each from {−10, −5, −2, 0, +2, +5, +10} %.
2. It simulates each sequence over `horizon_s`, including the motor speed and
   the minimum and maximum opening.
3. It scores each sequence: tracking error² (the last minute weighted ×5) +
   `lambda_travel`·travel % + `start_cost`·motor starts + 1000·(excess over
   setpoint + `margin_k`)².
4. It checks the limit a second time in a **"boiler refires in 60 s"**
   scenario (supply back at the recent maximum). The limit always uses a
   non-negative bias, so a model that reads too cold never loosens it.
5. It executes only the first move. The next plan starts from the new
   measurements (receding horizon).

While the boiler pauses (supply below setpoint + `supply_margin_k`), and while
the supply is rising faster than 0.05 K/s (burner restart, end of DHW), the
plan may close but never open. The boiler-role sensor lags the water by about
75 s, so during a rise any opening is computed from a supply that is too cold,
and after DHW the boiler water is hotter than any recent value. Offline this
removed every DHW-related fallback.

During the warm-up flush the controller holds `flush_max_pos` and does not run
its model; it starts from real return-water measurements after the flush.

**Heat-up boost.** For `boost_s` after a floor-pump start or a setpoint
increase, moves cost `boost_scale` × their normal price, and every minute of the
window counts the same (no end-of-window weighting). The planner may then open
past the steady position and back off as the loop warms up. Offline, with
identical boiler conditions, reaching setpoint − 1 K took 2.9 instead of 9.6 min
at 35 °C and 5.7 instead of 44.7 min at 40 °C, for one extra motor start.

**Cost on the ESP32:** about 55 ms per plan (measured, classic ESP32), every
30 s or on a trigger, inside the 2 s L2 tick.

## 3. Supervisor and cautious fallback

These limits are compiled in. A parameter update cannot relax them.

| Anomaly | Condition |
|---|---|
| `model_bias` | \|bias\| > 5 K for 120 s (for example a large position error) |
| `prediction_miss` | measurement > 2 K away from the plan for 300 s despite re-plans |
| `overshoot` | T_fwd ≥ reference + `mix_overshoot_delta_c` for 10 s, or ≥ reference + 6 K immediately. The reference follows a setpoint increase at once but a decrease only at 1 K/min, so lowering the setpoint does not count as an overshoot. It is never below the temperature reachable at the minimum opening (return + capped hot fraction), so an unreachable setpoint is not a fault either |
| `numeric` | no finite plan |
| `manual` | `mode = cautious` requested |

Any anomaly starts **homing** first. The **cautious** controller then uses only
the measured forward temperature, the setpoint and the human-owned opening window:

* It starts at the minimum opening.
* It opens +3 % only if T_fwd < setpoint − 1.5 K and the supply is ≥ setpoint + 3 K.
* It closes −4 % if T_fwd > setpoint + 1 K.
* It waits 240 s between these moves.
* It closes −8 % immediately (at most once a minute) if T_fwd > setpoint + 2.5 K.
* It never opens above 70 %.

**Automatic retry:** the controller returns to `mpc` after 30 min within
±1.5 K, at least 60 min after the anomaly. If another anomaly follows within
2 h of a retry, it stays in `cautious` until `mode = mpc` is sent.

Event codes: 1340 anomaly and homing, 1341 homed (cautious active), 1342 MPC
resumed, 1343 parameters accepted, 1344 parameters rejected, 1345 mode change.

## 4. Parameter sets (observer interface)

There are four sets, selected automatically by the radiator pumps:

| Set | Pumps |
|---|---|
| `floor` | floor only |
| `rad1` | floor + radiator 1 |
| `rad2` | floor + radiator 2 |
| `both` | floor + radiator 1 + radiator 2 |

Radiator starts are not predicted. When the pumps change, the controller
switches to that scenario's set and re-plans immediately.

Defaults are the identified values from `floor_simulator/log_analysis`
(Step 4 law, 40 s delay, 34.6 s sensor lag, 75.3 s boiler-sensor lag). The
radiator sets use the Step 4 radiator multiplier (0.793) on the law.

| Field | Bounds | Default | Meaning |
|---|---|---|---|
| `knots_pct[11]` | 0–100, increasing | Step 4 | valve position knots |
| `xk[11]` | 0–1, non-decreasing, span ≥ 0.05 | Step 4 | hot fraction at the knots |
| `delay_s` | 0–150 | 40 | transport delay |
| `tau_fwd_s` | 1–600 | 34.64 | forward-sensor lag |
| `tau_inc_s` | 0–600 | 75.27 | boiler-sensor lag (lead) |
| `horizon_s` | 300–1200 | 900 | prediction window |
| `replan_s` | 4–300 | 30 | periodic re-plan |
| `trigger_k` | 0.1–5 | 0.5 | prediction miss forcing a re-plan |
| `lambda_travel` | 0–10 | 0.1 | cost per % travel |
| `start_cost` | 0–50 | 1.0 | cost per motor start |
| `margin_k` | 0.5–10 | 2.5 | soft limit above setpoint |
| `bias_alpha` | 0–1 | 0.1 | bias filter gain per 10 s |
| `supply_margin_k` | 0–20 | 3 | supply needed to act |
| `boost_s` | 0–3600 | 600 | heat-up window after a floor-pump start or a setpoint increase > 1 K |
| `boost_scale` | 0–1 | 0 | travel and start cost multiplier inside the window (0 = moves are free) |
| `boost_error_k` | 0–20 | 0 (off) | also boost when \|setpoint − T_fwd\| exceeds this with a usable supply. Off by default: offline it doubled travel without faster recovery after DHW (the supply is already hot then) |

An update is a JSON object. Fields that are left out keep their value:

```json
{"set": "rad1", "xk": [0.016, 0.07, ...], "delay_s": 45, "persist": true}
{"set": "all", "reset": true}
```

* MQTT: `<base>/cmd/mpc/params` (payload above), `<base>/cmd/mpc/mode`
  (`mpc` | `legacy` | `cautious`). Results go to `<base>/state/mpc/ack`. Each set
  is retained at `<base>/state/mpc/params/<set>`, and the status is published to
  `<base>/state/mpc` every 5 s.
* REST: `GET /api/mpc`, `POST /api/mpc/params`, `POST /api/mpc/mode {"mode": ...}`.
  `/api/state` carries the same status under `mpc`, including the predicted T_fwd
  trajectory.

Once a command secret is set, parameter updates and MQTT mode commands must
be HMAC-signed envelopes (see `security.md`); plain JSON is then
rejected. A rejected update changes nothing (all or none), and the event log
names the offending field. Accepted sets are stored in their own NVS blob (`hpnmpc`), so
the main configuration blob is untouched.

## 5. Learned predictor (planned)

The planner only needs a function that predicts `T_fwd` for a candidate move
sequence. The physics model is one implementation of it. A learned model can
replace it later, behind the same supervisor and fallback:

* The observer trains a small network offline from the logged telemetry
  (`hil_timeseries`, the live MQTT slow log). Inputs are recent T_inc, T_ret,
  T_fwd, valve position and the planned moves. The output is the T_fwd
  trajectory. This is supervised system identification. Reinforcement learning
  can then tune the cost weights, or a policy, against the simulator, not
  against the house.
* The weights are sent like a parameter set and checked against held-out data
  by the observer before they are sent. On the device they are bounded by the
  same anomaly checks.
* The over-temperature limit, the anomaly supervisor and the cautious fallback
  stay model-free, so a bad network can at worst cause a fallback.

## 6. Diagnostics

`GET /api/system/coredump` returns the last panic stored in flash (task, PC,
backtrace). Decode it with the matching `firmware.elf`:
`xtensa-esp32-elf-addr2line -pfiaC -e firmware.elf <addresses>`.
`POST /api/system/coredump/clear` erases it.

## 7. Network upload

```bash
pio run -e esp32dev_sim_ota -t upload --upload-port <host:port>     # application
pio run -e esp32dev_sim_ota -t uploadfs --upload-port <host:port>   # portal files
```

The upload logs in with `HPN_PORTAL_USER` / `HPN_PORTAL_PASSWORD` (default
admin / admin) and signs the upload; see `security.md`.

`-t uploadfs` sends the LittleFS portal image the same way (`?target=fs`).
The device reboots after each upload. This posts the image to `POST /api/update` on the device's port 80, through
any HTTP path: a reverse proxy, or an SSH tunnel to port 80. There is no
ArduinoOTA/espota listener, so no other port is needed.
