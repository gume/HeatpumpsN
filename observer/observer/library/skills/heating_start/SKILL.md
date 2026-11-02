---
title: Mixed circuit heating starts
description: Reviews heating starts of a mixed (valve-controlled) circuit - time to reach the setpoint, overshoot, valve behaviour, controller events.
tools: [debounce_binary, on_periods, setpoint_tracking]
---
Goal: how well a mixed circuit reaches its setpoint after a heating start, and
whether the valve control behaves.

1. `house_context`: the mixed circuit, its pump, supply (after the mixer) and
   return sensors, the valve position, the setpoint, the heat source sensor;
   the owner knowledge (targets, known changes such as firmware updates).
2. Window: `data_coverage` on those signals; use what exists (several days
   if available), and say which part you used.
3. In `run_python`: `obs.data.series` at 1 min for pump, supply, return,
   valve position, setpoint, source temperature.
   * starts: `tools.call("debounce_binary", pump)` then
     `tools.call("on_periods", state, min_off_s=600)`; a start is an
     on-period after at least 10 minutes off;
   * per start: `tools.call("setpoint_tracking", supply, setpoint, starts,
     companions={"valve": valve, "return": ret})`;
   * a plot per start (supply, setpoint, valve) into the workspace.
4. `get_events` over the window: events near each start (fallbacks, homing,
   sensor warnings, safety limits). Separate behaviour before and after any
   change the owner knowledge mentions.
5. Judge: more than 20 minutes to get within 2 K, overshoot above 2 K, a
   valve that stays nearly closed while the supply is far below the setpoint,
   repeated fallbacks.
6. `finish`: a table per start, findings with evidence, proposals
   (parameter changes are proposals for the owner, never applied).
