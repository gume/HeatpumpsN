---
title: Sensor health check
description: Finds stale, stuck, noisy or implausible temperature sensors and sensors that disagree with their redundant partner.
defaults:
  window: -24h
max_steps: 24
---
Goal: a list of temperature sensors that need attention, with evidence.

1. Read `house_context` for the circuits, measurement points and any
   redundancy relations (two sensors measuring the same point, e.g. the
   controller's and EMS-ESP's sensor on the same pipe).
2. `find_signals` with `quantity: temperature` (stored signals). Group them:
   heating circuits (supply/return per circuit), boiler, DHW, rooms, outdoor.
3. For each sensor (or the important ones, if there are many) call
   `signal_features` over the window. Flag:
   * **stale**: last sample much older than its usual interval, or a
     `max_gap_s` far above `median_interval_s` (more than 10x and > 15 min);
   * **stuck**: `longest_constant_s` above 3 h while related signals moved
     (a room sensor can legitimately be flat at night; compare with others);
   * **implausible**: values outside the physical range for its role
     (rooms 5–35 °C, outdoor −30–45 °C, heating water 5–90 °C), or jumps;
   * **noisy**: stdev unusually high for its role.
4. For redundant pairs, fetch both with `get_series` (1m mean) and compute the
   offset and its spread with `run_python` using `skill/health.py`
   (`pair_offset(csv, a, b)`). A steady offset of more than 1.5 K or a spread
   above 1 K is a finding.
5. `finish` with a table: sensor, problem, evidence (values and times),
   suggested action. Mention sensors you could not check and why.
