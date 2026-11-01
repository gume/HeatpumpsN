# Controller issues

Open work on the controller firmware, portal, simulator and Home Assistant
side, to be handled later. Newest first within each group. Remember the
working agreements in [`HANDOVER.md`](HANDOVER.md) §0: no S2 upload without
the owner's "go", bench off.

## To do

### F1. Floor setpoint input: 15–50 °C in 0.5 steps

HA shows the floor setpoint number with min 5, max 80, step 0.5. Restrict it
to **15–50 °C, step 0.5**.

* HA discovery: `publish_setpoint_number()` in `src/mqtt_client.cpp`
  (`min`, `max`; `step` is already 0.5).
* The command check for `cmd/floor_setpoint` in the same file accepts
  5–80 °C (event 1804 on rejection): use the same limits, ideally from one
  constant, and decide whether a value off the 0.5 grid is rounded or rejected.
* Check the portal/config for the fallback (`floor_setpoint_shadow`,
  `setpoint_default` in `src/config.cpp`) so a saved value outside the range
  cannot be used (clamp on load).
* HA keeps the retained command: a value outside the new range already on the
  broker is then rejected after the upgrade; mention it when deploying.

### F2. Mixer relays show 0 h runtime in Diagnostics → Relays

The open/close relays of the mixer always show 0 h, while the pumps and the
heater accumulate runtime. Cause found in the code: `update_counters()` in
`src/web_portal.cpp` adds runtime only for the heater and the three pumps;
`update_mixer_counters_locked()` counts only cycles (from
`l0::mixer_relay_ops()`), never on-time.

* Mixer pulses are often shorter than a second, so the pumps' whole-second
  accounting on a sampled state would miss them: accumulate milliseconds of
  motor-on time per direction, best in L0 next to the relay operation counter
  (it sees every edge), and add whole seconds to `runtime_s`.
* Check that the UI shows hours with enough precision (or minutes) for the
  mixer, and that the NVS counter blob format does not change (no migration).

## Known issues and later work

1. **Network latency on the S2**: pings of 0.2–5 s before the memory fixes;
   not re-measured since.
2. **One unexplained interrupt-watchdog reset** on the bench (first boot of one
   image; the same image ran cleanly afterwards; no coredump).
3. **Home Assistant keep-alive**: install the blueprint
   `homeassistant/blueprints/automation/heatpumpsn/heating_keepalive.yaml`, one
   automation per switch (e.g. 120 s; the controller's timeout is 600 s).
4. **Per-circuit pump after-run**: separate timers exist, the configuration
   still has one `pump_stop_after_heating_s`. Decide.
5. **Mixer position shows negative during the boot homing stroke** (cosmetic;
   the position is set to 0 when the stroke ends, HA gets 0–100 %). Clamp at 0.
6. **LED 0 blue for the AP / captive portal** (plan §1.2) not implemented.
7. **v1.x hardware**: schematic open items, then PCF8574, ADS1115 and the
   interlock fault line on real hardware, current calibration.
8. **Mixer encoder over I2C** (`POS_SRC_I2C`) not implemented; the MQTT encoder
   is used for cross-checks and resync only.
9. `lastWatchdog` in `/api/status` keeps the last withhold across resets (the
   old false alarm until a new one replaces it).
10. **S2 upgrade checklist leftovers** (`docs/esphome-upgrade.md`): change the
    factory passwords (5), verify sensor roles while heating (6.2), relay
    counter preset (7), configuration export as backup (8).
