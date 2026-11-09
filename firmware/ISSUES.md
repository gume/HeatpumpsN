# Controller issues

Open work on the controller firmware, portal, simulator and Home Assistant
side, to be handled later. Newest first within each group. Remember the
working agreements in [`HANDOVER.md`](HANDOVER.md) §0: no upload without the
owner's explicit "go", and the only board is the live S2 (extra care).

## To do

1. **Owner's password decision** (2026-11-09): the factory `admin/admin`
   stays during development; the site's network access is restricted instead.
   Revisit before the development phase ends.
2. **Mixer parameters, the owner's return** (2026-11-09): the current set
   stays; the owner will come back to it. The point to answer then is "if MPC
   does not work with the current parameters there is no use in keeping
   them". As of 2026-11-09 MPC does work (see below), so nothing was changed.
   The observer's re-identification proposal `edd054c886` fits the held-out
   slow log better (1.346 -> 0.801 K) but loses the closed-loop backtest
   (~2x starts/reversals); it stays unapproved.

Done and on the S2 since 2026-11-09 (uploaded 18:40 firmware, 18:44 portal,
both from `7eb5eaa`; visit at `http://localhost:30080`):

* three-strike MPC retry block with the 20 min cautious stable run, the
  permanent blocked-retry banner and the **Unblock and resume MPC** button;
* the boot homing of the mixer always raises `mix_no_response` for a moment
  ("mixer commanded but the external encoder did not respond"); it clears when
  the encoder responds and is warning-only. It is a recurring condition in the
  logs, not caused by the upload. Retry/anomaly counters restart at 0.

Evidence that MPC works with the current parameters (checked 2026-11-09
before the upload): over 30 days the mode signal is only `mpc` or `cautious`
(never `legacy`); the S2 had computed 10 008 plans and 4 888 triggers with
`preferredMode: mpc`; of 9 anomalies and 8 retries every retry survived and
MPC resumed. So the parameters are not the reason to replace the controller
logic, and the owner's "no use to keep them" condition is not met yet.

Done and on the S2 since 2026-11-01 (commit `9fcb58c`):

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
