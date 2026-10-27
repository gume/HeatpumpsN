# Firmware change log — real-heating readiness (commit `bea823e`)

**Date:** 2026-10-26
**Base commit:** `36f756d` ("firmware: add configurable mixer opening ceiling (85% default)")
**Commit:** `bea823e` ("firmware: real-heating readiness (config v2, mixer window, supervision)")
**Device:** remote ESP32-D0WDQ6 bench board flashed with `esp32dev_sim`
(HHN 1.0 emulation), Wi-Fi `Testing`, controller `http://192.168.2.14`,
broker `smcl.tmit.bme.hu:31883`.

> **Scope.** This is the engineering log for one change set, not the project
> handover. Start at [`../HANDOVER.md`](../HANDOVER.md) for current state, access,
> workflow, architecture and next steps; come back here for the details of this
> commit.

> **Summary.** This session adds the remaining bench-verifiable pieces for
> real-heating operation: shared pump-current calibration with a protection
> switch, a configurable mixer minimum/dynamic maximum, a soft setpoint+Δ
> reaction, warning-only mixer anomaly indications, sensor-role verification and
> an optional inferred-DHW fallback. The configuration blob moved to **v2**; a
> v1 blob is migrated by preserving its 1288-byte prefix, so the bench settings
> survived unchanged. Both firmware environments build; the emulation image and
> filesystem are flashed and the runtime suite passes. Upload the filesystem as
> well as the application because `data/js/app.js` and its `.gz` sibling changed.

---

## 1. Objective

The controller was functionally complete on the bench but several parameters
were hard-coded or unverified for the real plant:

1. The pump-current calibration was a fixed legacy fit, and there was no way to
   run without current-based protection while the shunt is uncalibrated.
2. Mixer stall detection based on supply temperature was unreliable and could
   stop a healthy valve.
3. There was no check that the three 1-Wire sensors were assigned to the right
   hydraulic roles.
4. The old controller used a 40 % minimum and a temperature-dependent ceiling;
   neither was configurable.
5. A configurable setpoint+Δ reaction and an optional DHW inference were
   needed for plants without a DHW meter.

This session implements those without changing heater control, anti-seize or the
existing safety hierarchy.

---

## 2. What changed

### 2.1 Config v2 and NVS migration

`firmware/include/hpn/config.h` — new fields appended after
`mix_max_open_pct`; `sizeof(Config)` is now **1348** bytes (v1 was 1288):

* pump calibration: `pump_current_w_per_v`, `pump_current_offset_v`,
  `pump_power_check_enabled`;
* mixer window: `mix_min_open_pct`, `mix_max_dynamic`, `mix_max_below_pct`,
  `mix_max_above_pct`, `mix_max_switch_c`, `mix_max_hysteresis_c`;
* overshoot: `mix_overshoot_delta_c`, `mix_overshoot_recovery_c`,
  `mix_overshoot_target_pct`;
* sensor roles: `sensor_role_check_enabled`, `sensor_role_tolerance_c`;
* inferred DHW: `dhw_infer_enabled`, `dhw_infer_drop_c`, `dhw_infer_window_s`,
  `dhw_infer_recovery_c`, `dhw_infer_max_s`.

Helpers `mixer_ceiling()`, `mixer_minimum()` and
`mixer_effective_ceiling(config, above)` are the single source of truth for the
limits. The effective ceiling is `min(absolute, dynamic)`.

`firmware/src/config_store.cpp`:

* `kVersion = 2`, `kV1ConfigSize = 1288`;
* a v1 blob is CRC-checked, copied over current defaults and sanitized, then
  returned as `ConfigLoadResult::Migrated`;
* `main.cpp` immediately saves the migrated blob, so the upgrade is one-time;
* every new field is range-checked in `sanitize_config()`, and a minimum above
  the usable ceiling is clamped instead of rejecting the stored settings;
* `sizeof(Config)` is statically asserted so a future field addition must bump
  the version and extend the migration path.

The bench v1 blob migrated on the first boot after flashing; subsequent boots
load v2. No NVS corruption (`501`) was reported.

### 2.2 Pump current calibration and protection switch

`firmware/src/l0_io.cpp` now samples the selected channel and computes

```
watts = (rms_volts - pump_current_offset_v) * pump_current_w_per_v
```

with the shared calibration. The legacy values remain the defaults
(`0.004 V`, `588.2 W/V`) but are editable under **Settings → Hardware → Pump
current calibration**. When `pump_power_check_enabled` is false, L1 still
publishes the measurements but clears the dry-run/welded counters and never
trips on them. The unverified-polarity TODO on the output extender was replaced
with the confirmed carrier description (see §2.6).

### 2.3 Mixer window: minimum, dynamic maximum, overshoot

`firmware/src/l1_safety.cpp`:

* maintains the hysteresis side of the two-point maximum from the boiler-side
  temperature and publishes the effective ceiling in `state.mix_ceiling`;
* uses that ceiling for every opening move, including free-run sweeps, and
  updates it live when the config changes;
* adds warning-only motion supervision: `WARN_MIX_MOTION_OVERRUN` after
  `travel_time_ms + 15 s`, and `WARN_MIX_NO_RESPONSE` when a valid external
  encoder does not move within 3 s of a commanded move. Neither stops the
  motor.

`firmware/src/l2_control.cpp`:

* enforces `mix_min_open_pct` only during normal floor-demand control; the
  no-demand path always closes to 0, and DHW/flush/safety/homing/overshoot are
  exempt;
* implements the soft setpoint+Δ reaction: at `setpoint + mix_overshoot_delta_c`
  it drives to `mix_overshoot_target_pct` and holds until
  `setpoint - mix_overshoot_recovery_c`, raising `WARN_MIX_OVERSHOOT`;
* exposes `state.mix_overshoot` to the portal.

### 2.4 Sensor-role verification

New `firmware/include/hpn/sensor_roles.h` / `firmware/src/sensor_roles.cpp`.
The check is **only measurable while `heater_phys && pump_floor_phys`** and all
three readings are valid. It verifies:

```
boiler >= forward >= return   (within sensor_role_tolerance_c)
```

`/api/sensors/roles` returns the raw temperatures, the three comparisons,
`measurable`, `plausible` and a human-readable summary. L1 raises
`WARN_SENSOR_ROLE_SUSPECT` (error code 603) after 60 s of a persistent
inversion and clears it after 30 s of a plausible order. Outside the
measurable condition it reports “not measurable” and clears the warning.

### 2.5 Optional inferred DHW

When `dhw_infer_enabled` is true and there is no meter, a boiler-temperature
drop of `dhw_infer_drop_c` within `dhw_infer_window_s` while the heater is on
sets `dhw_inferred`. Recovery within `dhw_infer_recovery_c` of the pre-drop peak
ends it. If it lasts longer than `dhw_infer_max_s`, the floor is released and
`WARN_DHW_INFER_TIMEOUT` / error 604 is raised; the warning stays latched until
recovery or a new heater cycle, so it cannot clear and re-latch on the same
drop. `dhw_active` is the combination
`dhw_meter_active || dhw_inferred`, so a configured meter always wins.

### 2.6 IO-extender facts and documentation

The HomeNet Heatpumps 1.0 carrier does not use a bare 74HC595. Its serial output
extender pulls the selected outputs to **GND** at power-on/reset; the relay board
is **active-high**, so `0x00` is the correct safe byte and
`kActuatorOnLevel = HIGH`. This is now documented in
`firmware/docs/hhn-1.0.md` and the firmware comments; the old “polarity assumed,
confirm” TODO was removed. The boot log line now says `output extender` instead
of `74HC595`.

### 2.7 Portal and UI

`firmware/src/web_portal.cpp`:

* `/api/config/safety` and `/api/config/hardware` expose and validate all new
  fields, including cross-field checks;
* `/api/state` reports the effective/absolute ceiling, minimum, dynamic flag,
  overshoot latch, pump-protection switch, meter/inferred DHW and role data;
* `/api/sensors/roles` is registered;
* new alarm bits/error codes are added to the state JSON and error text;
* Test Mode target clamping uses the live effective ceiling.

`firmware/data/js/app.js` (+ regenerated `.gz`):

* Settings → Safety: mixer operating window, overshoot, sensor-role and inferred
  DHW sections;
* Settings → Hardware: pump current calibration and protection switch;
* Hardware → Sensor identities: **Verify roles** button and a comparison table;
* Dashboard/Test page: minimum, effective and absolute maximum, and the
  overshoot reaction state.

### 2.8 Watchdog and HTTP response hardening

The first bench run after flashing ended in a `task-watchdog` reset, and two
follow-up runs failed with an HTTP timeout and with a truncated
`/api/log/events` body. Both had the same origin:

* **AsyncTCP was feeding the plant watchdog.** `AsyncTCP` compiles its
  `esp_task_wdt_add()`/`esp_task_wdt_reset()` calls unless
  `CONFIG_ASYNC_TCP_USE_WDT` is 0, so the `async_tcp` service task was a second
  watchdog entry. The ESP task watchdog only restarts the timer when *every*
  entry has been reset, so a long HTTP callback (or a stalled TCP stream) made
  the controller panic even though `T_safety`/`T_io`/`T_control` were healthy.
  The task watchdog is now fed only by `T_io`, matching the design rule that the
  network stack must never be able to reset the plant.
* **Large JSON responses grew one byte at a time.** `AsyncResponseStream`
  starts with a 1460-byte `cbuf` and `resizeAdd()`s a whole new ring buffer for
  every byte past the end, so the ~10 KB event-log and slow-log responses
  performed thousands of reallocations inside the network task. That blocked
  the HTTP server long enough to time out the test client, and when the heap
  could not grow the payload mid-serialisation the response was silently
  truncated (the observed invalid JSON at byte 8713). `send_json()` now uses
  `measureJson()` to size the response buffer exactly, so it is allocated once.

`T_io` also logs a rate-limited `1900`/`1901` event with the safety/io/control
ages whenever it withholds a watchdog feed, so a future reset is diagnosable
from the captured boot log instead of only showing `reset reason:
task-watchdog`.

### 2.9 Docs and plans

Updated `firmware/README.md`, `firmware/docs/hhn-1.0.md`,
`firmware/docs/io-emulation.md`, `tests/README.md`,
`plans/controller node/01_config_and_contracts.md`, `03_safety_layer_l1.md` and
`04_control_and_coordination_l2.md`.

---

## 3. Design decisions / caveats

* **L1 remains authoritative.** L2 and the portal clamp for coherent targets and
  early rejection, but L1 never opens past the effective ceiling.
* **The plant watchdog is fed only by `T_io`.** Network code (including
  AsyncTCP's own service task) is excluded, so HTTP/MQTT activity cannot reset
  the outputs. A withheld feed is logged with the task ages.
* **JSON responses are sized before serialisation.** `AsyncResponseStream`
  grows one byte at a time; pre-sizing with `measureJson()` keeps the large log
  endpoints to a single allocation and prevents truncated bodies.
* **Minimum is an L2 normal-control rule.** Closing, safety close, homing, DHW,
  the flush cap and the no-demand close are all allowed to reach 0 %.
* **Dynamic maximum is opt-in.** Defaults are 90 % below / 70 % above a 50 °C
  switch with 2 K hysteresis, but `mix_max_dynamic` is false until enabled.
* **Overshoot is soft.** It raises a warning and reduces the opening; the hard
  over-temperature rule in L1 is unchanged.
* **Mixer anomaly indications are warning-only.** A seized valve, a frozen
  encoder and a wiring fault are indistinguishable from this data, so the
  controller does not stop the motor on these warnings.
* **Sensor roles only while heating.** An idle plant cannot prove the order, so
  the check reports “not measurable” rather than a false result.
* **Inferred DHW is opt-in.** Meter-based DHW remains the default and is never
  overridden.
* **Migration preserves old settings.** The v1 prefix is copied verbatim over
  current defaults; only the appended fields get defaults.

---

## 4. Verification performed

Builds (PlatformIO, release):

* `esp32dev_sim` → **SUCCESS**, RAM 25.2 % (82 436 B), flash 77.1 %
  (1 263 552 B of 1 638 400 B).
* `esp32s2` → **SUCCESS**, RAM 28.9 % (94 740 B), flash 76.0 %
  (1 245 467 B).
* Only the pre-existing `wifi_client.cpp` `-Wformat` warning is present.
* `node --check firmware/data/js/app.js` passes; `app.js.gz` regenerated with
  `gzip -9 -n`.
* `xtensa-esp32-elf-nm` confirms the AsyncTCP object no longer references
  `esp_task_wdt_add`/`esp_task_wdt_reset`.

Bench (`esp32dev_sim` application + LittleFS):

* `python3 tests/runtime_check.py --serial` → **PASSED: 65 passed, 0 failed,
  0 skipped**.
* Without `--serial` → **PASSED: 58 passed, 0 failed, 0 skipped**, five times
  back-to-back with an attached serial monitor and no reset; uptime grew
  monotonically and `minFreeHeap` stayed above 97 KB.
* `/api/log/events` with a full 96-entry ring (10 657 B) was fetched 20 times:
  every body parsed, worst latency 0.22 s, and no `Failed to allocate` was
  logged.
* The boot log no longer contains `task_wdt: ... task not found`; the only
  watchdog line is `[rtos] task WDT armed at 9000ms`.
* The bench v1 config was migrated and preserved: the saved broker, Wi-Fi,
  ceiling and timing values were still in effect after flashing and rebooting.
* New checks:
  * config contract exposes all mixer-window, overshoot, role, inferred-DHW and
    pump-calibration fields;
  * `/api/sensors/roles` reports measurable+plausible with safe temperatures
    and measurable+suspect after inverting boiler/return;
  * Test Mode target above the ceiling returns HTTP 400; a mid-move ceiling
    change stops the valve at the new limit;
  * the boot log shows the saved config loading, no NVS corruption, no
    Preferences errors and no crash markers.
* Boot log lines include `[l1] mixer opening limit 85% (absolute)`,
  `[l1] dynamic maximum disabled` (default) and
  `[l1] normal floor opening minimum 40%`.

---

## 5. How to flash and check

From `firmware/` (the attached bench board is a plain ESP32, so use
`esp32dev_sim`; `esp32s2` is compile-only for now):

```bash
pio remote run --force-remote -d . -e esp32dev_sim --target uploadfs
pio remote run --force-remote -d . -e esp32dev_sim --target upload
python3 ../tests/runtime_check.py --base-url http://192.168.2.14 --serial
```

`tests/flash_and_check.sh --serial` (from the repository root) does the build,
flash and checks in one step; set `HPN_UPLOAD_FS=1` to include the UI assets.

Always bound the serial monitor with `timeout`; never run an unbounded
`pio remote device monitor`. The suite's `--serial` check does this for you.

---

## 6. Files in this change

All files below are part of commit `bea823e`:

```
firmware/HANDOVER.md
firmware/README.md
firmware/data/js/app.js
firmware/data/js/app.js.gz
firmware/docs/hhn-1.0.md
firmware/docs/io-emulation.md
firmware/include/hpn/config.h
firmware/include/hpn/config_store.h
firmware/include/hpn/l0_io.h
firmware/include/hpn/sensor_roles.h      (new)
firmware/include/hpn/state.h
firmware/platformio.ini
firmware/src/config.cpp
firmware/src/config_store.cpp
firmware/src/l0_io.cpp
firmware/src/l1_safety.cpp
firmware/src/l2_control.cpp
firmware/src/main.cpp
firmware/src/mqtt_client.cpp
firmware/src/sensor_roles.cpp            (new)
firmware/src/state.cpp
firmware/src/tasks.cpp
firmware/src/web_portal.cpp
plans/controller node/01_config_and_contracts.md
plans/controller node/03_safety_layer_l1.md
plans/controller node/04_control_and_coordination_l2.md
tests/README.md
tests/runtime_check.py
```

Untracked / unrelated and deliberately not part of this commit: `reviews/`,
the `simulation/` edits and the `simulation/sim_run_hil_*`, `test_run_*` and
`phys_sysid*` artefacts. They remain local; see `../HANDOVER.md` §10.

---

## 7. Open items

1. **ESP32-S2 hardware test is deferred** — the user will test the real HHN 1.0
   carrier later. The `esp32s2` environment builds but has not been flashed.
2. Calibrate `pump_current_offset_v` / `pump_current_w_per_v` with a clamp meter
   on the real plant before enabling `pump_power_check_enabled`.
3. Review the default dynamic-maximum switch point and the 40 % minimum against
   the real hydraulic behaviour.
4. Decide whether the warning-only mixer indications should ever escalate to a
   stop once the plant has a reliable encoder.
5. The simulator/HIL tooling has local, unrelated edits that were not reviewed
   or committed in this session.
