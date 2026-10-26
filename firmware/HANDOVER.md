# Firmware session handover — RAM logs, MQTT streaming, CRC32, HA error sensors, LEDs

**Date:** 2026-10-26
**Base commit:** `6ed1ff3` ("simulation")
**Working tree:** clean; all changes are committed (see §6).
**Device:** remote ESP32-D0WDQ6 bench board flashed with `esp32dev_sim`
(HHN 1.0 emulation), Wi-Fi `Testing`, controller `http://192.168.2.14`,
broker `smcl.tmit.bme.hu:31883`. The real HHN 1.0 carrier is `esp32s2`; the
physical LED order still needs a check on that board.

> **Read this first.** This session changed the NVS layout, the logging
> ownership and the MQTT entity set. Both `esp32s2` and `esp32dev_sim` build
> clean, and the `esp32dev_sim` image has been flashed and verified on the bench
> board (see §4). Upload the filesystem before the application because
> `data/js/app.js` and its `.gz` sibling changed.

---

## 1. Objective

1. Never store log messages in the MCU flash. Keep them in RAM circular buffers,
   with optional publishing to MQTT.
2. Add two Home Assistant text sensors: numeric `system_error_code` (`0` = no
   error) and the matching human-readable `system_error_text`.
3. Remove all NVS state migration and migration preparation. Add CRC32 to the
   NVS stores to detect corruption.
4. Add the missing status-LED control.

---

## 2. What changed

### 2.1 RAM-only logging (`hpn::logger`)

`firmware/include/hpn/logger.h`, `firmware/src/logger.cpp` now own both streams:

* event buffer: **96** records, detail up to 95 chars, sequence number, uptime,
  severity, code, latched flag;
* slow buffer: **180** samples at a **10 s** cadence (boiler/return/mix
  temperatures, dead-reckoned position, four physical relay states).

Nothing is written to flash. The web portal reads snapshots through
`logger::events_after()` / `logger::slow_after()`; its old `EventEntry`,
`SlowSample` and ring-buffer globals were removed. `web::log_event()` simply
delegates to `logger::log_event()`.

`T_logger` samples the slow log and emits a diagnostic event whenever
`system_error_code` or `active_alarms` changes. `logger::begin()` is called
**before** `l4::begin()` so web/MQTT startup events are not discarded by the
buffer reset.

Optional MQTT streaming (new config fields):

* `mqtt_log_enabled` (default **false**);
* `mqtt_log_slow_topic[96]`, `mqtt_log_event_topic[96]` — full-topic overrides;
  empty means `<base>/log/slow` and `<base>/log/event`.

Records are JSON lines (`seq`, `ts`, optional `wall`, plus the record fields)
and are published non-retained. Per-stream cursors advance only after a
successful publish, so a broker outage does not block logging and the retained
RAM backlog is sent after reconnection. Enabling the feature starts at the
current cursor, so it streams new records rather than an old backlog.

### 2.2 Home Assistant error sensors

`publish_error_sensors()` adds two diagnostic entities:

* `sensor.system_error_code` → `<base>/state/system_error_code`;
* `sensor.system_error_text` → `<base>/state/system_error_text`.

`format_error_text()` (in `state.cpp`) returns `No error` for code `0` with no
flags, otherwise the code name, relevant state detail and active warning/alarm
names. Both entities are published only when the code or alarm mask changes;
ordinary relay, pump, mixer and setpoint changes do not touch them. The same
text is exposed in the portal's live JSON (`safety.errorText`,
`system.errorText`) and displayed on the dashboard.

### 2.3 CRC32 NVS stores, no migration

`firmware/include/hpn/crc32.h` adds a portable IEEE CRC32. `config_store.*`
now stores one blob with `{magic, version, size, crc}` and version **1**. All
v1–v4 migration code and the append-only layout assumptions are gone.

Load result is one of `Loaded`, `Missing`, `Stale`, `Corrupt`:

* **Stale** — old-format blob. Discarded silently and compiled defaults used;
  no alarm because this is not data corruption.
* **Corrupt** — current-format blob fails the size/CRC check. Compiled defaults
  are used and `main.cpp` latches `WARN_NVS_CORRUPTED` +
  `ERR_NVS_DATA_CORRUPTED` (code `501`).

The relay counters (`hpnctr`, now version **2**) and the anti-seize schedule
(`hpnanti`, now a `{magic, next_day, crc}` blob) use the same pattern. Legacy
counter/anti-seize formats are discarded silently; current-format CRC failures
latch the same warning/error.

### 2.4 Status LEDs

New `firmware/include/hpn/led_status.h` + `firmware/src/led_status.cpp`, called
from `l4::tick()` after MQTT/web. L0 still owns the NeoPixel write. The animator
uses the plan 02 §1.2 mapping:

| LED | Meaning |
| --- | --- |
| 0 | green online; yellow breathing when Wi-Fi/MQTT is down; red blink on critical alarm |
| 1 | orange heater on; cyan DHW priority; purple blink anti-short-cycle hold-off |
| 2 | green floor pump running; cyan breathing flush; magenta ΔT collapse; red rapid blink hard over-temp |
| 3–4 | orange radiator pump running; red welded-relay or dry-run fault |

The frame is rebuilt at most every 50 ms and only written when it changes.
Unassigned LED GPIO remains a no-op.

### 2.5 UI and docs

* Network settings page adds the log enable checkbox and both topic overrides.
* Dashboard LEDs now mirror the physical animator, and the Safety card shows
  `Error text`.
* `app.js.gz` regenerated.
* Updated `firmware/README.md`, `firmware/docs/hhn-1.0.md`,
  `plans/controller node/01`, `02`, `06`, `web_interface.md` and
  `plans/mqtt_messaging_plan.md` to match the implementation.

---

## 3. Design decisions / caveats

* Logs are lost on reboot by design. There is no flash archive and no NVS ring.
* The error text sensor reflects **diagnostic posture**, not every actuator
  transition. A warning/alarm mask change or error-code change updates it.
* Old-format NVS blobs are discarded without an alarm. If the user sees code
  `501`, the data really failed its current-format CRC/size check.
* The `501` / `WARN_NVS_CORRUPTED` latch lasts for the current boot. A
  successful config save rewrites the blob but deliberately does **not** clear
  the live flag, because the counters or anti-seize blob may still be damaged.
  The next boot clears it once every persisted blob validates.
* MQTT log streaming is opt-in to avoid unexpected broker traffic. The web
  portal always has the RAM buffers.
* Counter/anti-seize writes keep their previous hourly/5-minute deferred
  schedules; only the blob format gained a CRC.
* `resolve_log_topic()` no longer takes an unused `Config` argument.
* The MQTT log payload buffer is 768 bytes. A record that cannot fit advances
  the cursor instead of stalling the stream forever.

---

## 4. Verification performed

* `pio remote run -e esp32s2` → **SUCCESS**, RAM 28.9 % (94 540 B), flash 75.3 %
  (1 234 327 B of 1 638 400 B).
* `pio remote run -e esp32dev_sim` → **SUCCESS**, RAM 25.1 % (82 204 B), flash
  76.5 % (1 253 676 B).
* No compiler warnings in either environment.
* `node --check firmware/data/js/app.js` passes; gzip regenerated.
* Flashed `esp32dev_sim` and verified on the bench ESP32-D0WDQ6:
  * boot: saved config loaded, no Preferences errors, no NVS `501`, the legacy
    anti-seize scalar discarded, MQTT connected and HA discovery published;
  * `python3 tests/runtime_check.py --serial` → **PASSED: 52 passed, 0 failed,
    0 skipped**; without `--serial` → **45/45**;
  * MQTT log streaming verified on the default and override topics;
  * HA `system_error_code` / `system_error_text` verified `0 → 202 → 0` and
    `0 → 201 → 0`, including warning text without a double space;
  * all five LED states verified through the emulation model, including the red
    critical blink and the purple anti-short-cycle blink.
* Fixed during verification: a partial 8-byte `Preferences::getBytes()` read
  misclassified the 1112-byte legacy config blob as corrupt (false `501`); the
  legacy anti-seize `UInt` key made `getBytesLength()` log an error. Config and
  counter loads now read the whole blob, and the anti-seize loader checks the
  stored type and removes legacy scalars before writing a blob.

---

## 5. How to flash and check

From `firmware/` (add `--upload-port /dev/ttyUSB0` if auto-detection picks the
wrong port). The attached bench board is a plain ESP32, so use
`esp32dev_sim`; `esp32s2` fails with `This chip is ESP32, not ESP32-S2`.

```bash
pio remote run --force-remote -d . -e esp32dev_sim --target uploadfs
pio remote run --force-remote -d . -e esp32dev_sim --target upload
python3 ../tests/runtime_check.py --base-url http://192.168.2.14 --serial
```

`tests/flash_and_check.sh --serial` (from the repository root) does the build,
flash and checks in one step.

After boot, check:

1. `[config] loaded saved configuration from NVS` or
   `format differs ... using compiled defaults` (no `CRC/format check failed`
   on a normal first boot after the format change).
2. Portal → Dashboard shows `Error text: No error` and the five LEDs match the
   table above.
3. Settings → Network & MQTT: enable log streaming; watch `<base>/log/event`
   and `<base>/log/slow` for JSON lines.
4. HA discovers `System error code` and `System error text`; force a warning
   (for example a sensor fault on the bench) and confirm both update.
5. `/api/log/events` and `/api/log/slow` still return records from RAM.

Always bound the serial monitor with `timeout`; never run an unbounded
`pio remote device monitor`. The automated suite performs checks 1–5 and the
bounded boot capture; use it instead of repeating them by hand.

---

## 6. Working-tree state

Modified:

```
firmware/README.md
firmware/data/js/app.js
firmware/data/js/app.js.gz
firmware/docs/hhn-1.0.md
firmware/include/hpn/config.h
firmware/include/hpn/config_store.h
firmware/include/hpn/logger.h
firmware/include/hpn/state.h
firmware/src/config.cpp
firmware/src/config_store.cpp
firmware/src/l4_comms.cpp
firmware/src/logger.cpp
firmware/src/main.cpp
firmware/src/mqtt_client.cpp
firmware/src/state.cpp
firmware/src/tasks.cpp
firmware/src/web_portal.cpp
plans/controller node/01_config_and_contracts.md
plans/controller node/02_hardware_and_l0.md
plans/controller node/06_interfaces_and_logging.md
plans/controller node/web_interface.md
plans/mqtt_messaging_plan.md
```

New:

```
firmware/include/hpn/crc32.h
firmware/include/hpn/led_status.h
firmware/src/led_status.cpp
tests/README.md
tests/flash_and_check.sh
tests/runtime_check.py
```

Untracked and unrelated to this work: `reviews/` and the recent
`simulation/sim_run_hil_*.csv/png` files.

---

## 7. Open items

1. ~~Flash and run the checks in §5~~ — done on 2026-10-26 with
   `esp32dev_sim`; see §4.
2. ~~Confirm the HA error sensors appear with the expected names and update
   only on posture changes~~ — done; both entities update on code/alarm-mask
   changes, never on relay transitions.
3. Verify the physical LED order on a real HHN 1.0 board (the new mapping
   differs from the legacy ESPHome firmware). The bench run used the emulated
   carrier, so only the logical mapping is proven.
4. Decide whether the event log should also capture every `*_intended` vs
   `*_phys` divergence transition with more context; today the logger emits
   diagnostic-posture transitions and the existing lifecycle/sequence events
   from the portal/MQTT.
5. Consider a future explicit flash archive if logs must survive reboot; this
   was deliberately not implemented.
6. Resolved this session: a successful config save does not clear the NVS
   corruption latch early; the flag is held until the next valid boot so
   counter/anti-seize damage is not masked. Revisit only if a manual
   acknowledge/clear action is added to the portal.
