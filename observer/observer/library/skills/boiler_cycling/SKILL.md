---
title: Boiler cycling and modulation
description: Burner starts per hour, short cycling, modulation and flow temperature against the heat demand of the circuits.
tools: [debounce_binary, on_periods]
---
Goal: does the boiler short-cycle, and why?

1. `house_context`: the boiler, how the heat call is made, DHW priority.
2. `find_signals`: burner / heating-active state, burner power or
   modulation, flow temperature and its setpoint, DHW charging, the heat call
   relay and the circuit pumps. Window from `data_coverage` (a day at least).
3. In `run_python`: series at 1 min (raw for the burner state if it changes
   fast); `on_periods` on the burner state (debounce first if noisy):
   starts per hour, shortest and median on-period, duty cycle. A cumulative
   burner-start counter gives starts per hour from its increase.
4. Split on-periods into DHW and space heating (DHW charging flag); count
   heating on-periods shorter than 10 minutes per hour.
5. Relate short cycles to causes: minimum modulation above the demand (low
   flow-temperature need), flow setpoint reached quickly, DHW charging, pumps
   starting and stopping, controller anti-short-cycle events (`get_events`).
6. `finish`: starts per hour, share of short cycles, likely causes with
   evidence, proposals (boiler or controller settings) for the owner.
