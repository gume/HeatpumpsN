---
title: Floor circuit heating starts
description: Reviews every heating start of the mixed floor circuit - time to reach the setpoint, overshoot, mixer behaviour and controller events.
defaults:
  window: -24h
max_steps: 28
---
Goal: how well the mixed (floor) circuit reaches its setpoint after a heating
start, and whether the mixer control behaves.

1. `house_context`: identify the mixed circuit, its supply (mixer output /
   forward) and return sensors, the boiler or mixer-input temperature, the
   floor pump, the mixer position and the floor setpoint.
2. `find_signals` to get the exact ids (circuit, role and source help:
   the controller is source `heatpumpsn`).
3. `get_series` at 1m for: floor pump state, forward, return, mixer input /
   boiler, mixer position, setpoint. Use one file, e.g. `file: floor`.
4. With `run_python` and `skill/starts.py`: `find_starts(df, pump_column)`
   gives the starts (pump off -> on); `review_start(df, start, ...)` gives
   for each start the time until the forward is within 2 K of the setpoint,
   the overshoot above the setpoint, the mixer position after 5 and 15
   minutes and the return temperature. Save a plot per start (PNG).
5. `get_events` over the window: controller events near each start (MPC
   fallbacks, homing, encoder warnings, safety limits).
6. Judge: a start that needs more than 20 minutes to get within 2 K, an
   overshoot above 2 K, a mixer that stays nearly closed while the forward is
   far below the setpoint, or repeated fallbacks are findings.
7. `finish`: a table per start (time, minutes to setpoint, overshoot,
   mixer %), the findings with evidence, and proposals (parameter changes are
   proposals for the owner, never applied).
