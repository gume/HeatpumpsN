# Controller issues

Open work on the controller firmware, portal, simulator and Home Assistant
side, to be handled later. Newest first within each group. Remember the
working agreements in [`HANDOVER.md`](HANDOVER.md) §0: no S2 upload without
the owner's "go", bench off.

## To do

1. **Upload the 2026-11-09 firmware + portal** (waiting for the owner's
   explicit go). It brings the three-strike MPC retry block (20 min stable
   run), the non-dismissible blocked-retry banner and the **Unblock and
   resume MPC** button. Checklist and images in
   [`HANDOVER.md`](HANDOVER.md) §2.2. Nothing is on the S2 yet.
2. **Owner's password decision** (2026-11-09): the factory `admin/admin`
   stays during development; the site's network access is restricted instead.
   Revisit before the development phase ends.

Done and on the S2 since 2026-11-01 (commit 9fcb58c):

* F1 floor setpoint 15–50 °C, 0.5 K grid (HA discovery, MQTT command snapped
  to 0.5, portal default, saved values clamped on load);
* F2 mixer relay runtime (energized time from L0, the running segment
  included; on v0.1 the direction relay stays on after an open move, so its
  runtime is the coil's energized time, not only motor time).

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
    factory passwords (5; deferred by the owner until development ends, see
    *To do* above), verify sensor roles while heating (6.2), relay counter
    preset (7), configuration export as backup (8).
