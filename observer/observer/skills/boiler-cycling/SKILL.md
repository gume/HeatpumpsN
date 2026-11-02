---
title: Boiler cycling and modulation
description: Burner starts per hour, short cycling, modulation and flow temperature against the heat demand of the circuits.
defaults:
  window: -24h
max_steps: 22
---
Goal: does the boiler short-cycle, and why?

1. `house_context`: the boiler, how the heat call is made (controller heater
   relay / circuit demands) and DHW priority.
2. `find_signals` (source `ems-esp`, role boiler/burner/dhw; the controller's
   heater relay and circuit pumps from source `heatpumpsn`).
3. `signal_features` on the burner/heating-active state and the heater relay:
   switch-ons, shortest and median on-period, duty cycle. Burner start
   counters (cumulative) give starts per hour from their increase.
4. `get_series` at 1m for burner power/modulation, flow temperature, flow
   setpoint, heating active, DHW charging, the heater relay and the pumps;
   with `run_python` split the on-periods into DHW and heating, and count
   heating on-periods shorter than 10 minutes per hour.
5. Relate short cycles to their cause: minimum modulation above the demand
   (low flow temperature need), flow setpoint reached quickly, DHW charging,
   pumps starting and stopping, controller anti-short-cycle events
   (`get_events`).
6. `finish`: starts per hour, share of short cycles, the likely causes with
   evidence, and proposals (boiler or controller settings) for the owner.
