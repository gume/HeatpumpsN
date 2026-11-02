---
title: Sensor health check
description: Finds stale, stuck, noisy or implausible sensors and redundant sensors that disagree.
tools: [resample_hold, pair_offset]
---
Goal: the sensors that need attention, with evidence.

1. `house_context`: circuits, measurement points, redundancy relations (two
   sensors on the same point), and what the owner knowledge says about
   sensors (what a signal really measures, known faults).
2. Choose the window yourself: `data_coverage` tells how much history there
   is; a day shows staleness and stuck values, a week or more shows drift.
3. `find_signals` (quantity temperature, then others the question needs).
   Group them: circuits (supply/return), boiler, DHW, rooms, outdoor.
4. Quick look per sensor with `signal_features`: stale (`max_gap_s` far above
   `median_interval_s`, or last sample old), stuck (`longest_constant_s` of
   hours while related signals moved; rooms can be flat at night: compare),
   implausible range for the role, noisy (stdev unusual for the role).
5. Redundant pairs in `run_python`: `obs.data.series` at 1 min for both,
   `tools.call("resample_hold", ...)` if one publishes on change only, then
   `tools.call("pair_offset", a, b, mask=<pump running>)` (compare only when
   the water flows past both). Offset > 1.5 K or spread > 1 K is a finding.
6. `finish` with a table: sensor, problem, evidence (values, times), action.
   Name sensors you could not check and why.
