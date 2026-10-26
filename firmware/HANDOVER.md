# Firmware session handover — configurable mixer opening ceiling

**Date:** 2026-10-26
**Base commit:** `7e079f7` ("firmware: RAM-only logs, MQTT streaming, CRC32 NVS, HA error sensors, status LEDs")
**Device:** remote ESP32-D0WDQ6 bench board flashed with `esp32dev_sim`
(HHN 1.0 emulation), Wi-Fi `Testing`, controller `http://192.168.2.14`,
broker `smcl.tmit.bme.hu:31883`.

> **Read this first.** The mixer can no longer be commanded above a configurable
> ceiling. L1 is the authoritative enforcement point; L2, the portal and the UI
> know the same value. Both firmware environments build clean, the
> `esp32dev_sim` image and LittleFS assets are flashed, and the bench run passes
> every runtime check (see §4). Upload the filesystem as well as the application
> because `data/js/app.js` and its `.gz` sibling changed.

---

## 1. Objective

The old controller never opened the heating mixer past **85 %**. A fully open
mixer closes too slowly when the boiler temperature rises suddenly, so the
extra range was a safety liability rather than useful headroom. This session
adds a configurable maximum opening:

1. New parameter `mix_max_open_pct`, default **85 %**, range 10–100 %.
2. L1 refuses to drive the mixer above it on **every** opening move.
3. L2 and the portal clamp their targets to the same value.
4. Configurable from the device (Settings → Safety & timeouts).
5. Existing NVS configurations keep working without losing settings.

---

## 2. What changed

### 2.1 Config and NVS layout

`firmware/include/hpn/config.h`

* `uint8_t mix_max_open_pct` is the final `Config` member.
* `mixer_ceiling(config)` returns the ceiling as a `float` (0.01–1.0), clamped
  defensively, and is the single source of truth for every layer.
* The field reuses the former tail padding of the v1 blob, so
  `sizeof(Config)` stays **1288 bytes**. `config_store.cpp` now asserts this
  statically; a future layout change must bump the blob version instead of
  silently invalidating stored settings.

`firmware/src/config.cpp` sets the compiled default to **85**.

`firmware/src/config_store.cpp` upgrades a loaded blob whose value is outside
10–100 (including `0`, the old padding byte) to 85 and logs
`[config] mixer opening limit defaulted to 85%`.

### 2.2 L1 enforcement (`firmware/src/l1_safety.cpp`)

`service_mixer()` is the only code that energizes the motor, so it owns the
hard limit:

* the requested target is normalized and clamped to the ceiling before any
  comparison;
* a normal opening move stops at the ceiling and the dead-reckoned position is
  clamped to it;
* a free-run request (maintenance sweep / homing) opening upward stops at the
  ceiling and clears `mix_free_run_dir` / `mix_free_run_until_ms`, so it cannot
  pulse against the cap on the following ticks;
* a backstop clamps `pos_est_dr` after every opening tick, covering any path
  that bypassed the earlier checks;
* closing moves, emergency close and end-stop homing to 0 % are unaffected.

Boot log: `[l1] mixer opening limit 85%` (actual configured value).

Lowering the ceiling while the valve is open drives it back down to the new
limit; raising it does not move the valve by itself.

### 2.3 L2 and portal

`firmware/src/l2_control.cpp`

* `service_mixer_control()` clamps `mix_target_pos` at the top of the function,
  so early returns (DHW hold, no floor demand, deadband hold) cannot report a
  target above the cap either;
* every computed and held target is clamped with `mixer_ceiling()`.

`firmware/src/web_portal.cpp`

* `/api/config/safety` exposes and validates `mix_max_open_pct` (10–100);
* `/api/state` adds `circuits.floor.mixerMaxPct`;
* Test Mode nudges are clamped to the ceiling; an explicit
  `{"mixer":"target","position":>cap}` is rejected with **HTTP 400** and
  responses include the active `max`;
* the mixer sweep sequence opens only to the configured ceiling and logs
  `Mixer sweep: opening to the configured limit`.

### 2.4 UI and docs

`firmware/data/js/app.js` (+ regenerated `.gz`):

* Settings → Safety & timeouts: **Maximum mixer opening** (10–100 %, step 1)
  with an explanation of the slow-close risk;
* Dashboard and Test page show **Maximum opening** next to the estimated and
  target positions;
* the Test page target slider is bounded by the live ceiling and the help text
  names it.

Updated `firmware/README.md`, `plans/controller node/01`, `03` and `04`.

---

## 3. Design decisions / caveats

* **L1 is authoritative.** Higher layers clamp for a coherent UI and to reject
  bad requests early, but a stale L2 target or a direct writer still cannot
  open the valve past the ceiling.
* **The ceiling is not a position.** It limits the commanded opening only.
  Position estimation, encoder resync, homing and closing remain unchanged.
* **Old NVS blobs stay valid.** The new byte was previously tail padding, so no
  version bump or migration is needed. A stored `0` is treated as "unset" and
  upgraded to 85.
* **No automatic return to the cap on boot.** If the valve is physically above
  the cap, L1 only refuses further opening; it does not force a close on boot.
  Normal control or a lower target will bring it down.
* **Emergency close wins.** `mix_emergency` still forces `0.0` and is exempt
  from the ceiling logic.

---

## 4. Verification performed

* `pio remote run -e esp32dev_sim` → **SUCCESS**, RAM 25.1 % (82 204 B),
  flash 76.6 % (1 254 440 B of 1 638 400 B).
* `pio remote run -e esp32s2` → **SUCCESS**, RAM 28.9 % (94 540 B), flash
  75.4 % (1 235 055 B).
* Only the pre-existing `wifi_client.cpp` `-Wformat` warning is present.
* `node --check firmware/data/js/app.js` passes; `app.js.gz` regenerated with
  `gzip -9 -n`.
* Flashed `esp32dev_sim` plus the LittleFS image on the bench ESP32-D0WDQ6.
* `python3 tests/runtime_check.py --serial` → **PASSED: 59 passed, 0 failed,
  0 skipped**; without `--serial` → **52/52**.
* New runtime checks:
  * `mix_max_open_pct` present in `/api/config/safety` (85 % on the bench) and
    `mixerMaxPct` matches it in `/api/state`;
  * temporary Test Mode, `travel_time_ms=4000`, target `0 %`, then target
    `100 %`; after the valve passed 15 % the ceiling was lowered to **40 %**
    mid-move. L1 stopped at **40.0 %, motion off**;
  * `/api/state` reported the new 40 % ceiling immediately;
  * `POST /api/test/command {"mixer":"target","position":0.8}` returned
    **HTTP 400**;
  * the test restores `mix_max_open_pct`, `travel_time_ms` and Test Mode;
    `/api/config/*` confirmed 85 % and 145 000 ms afterwards.
* Boot log contains `[l1] mixer opening limit 85%`; the existing config blob
  loaded with no NVS corruption and no Preferences read errors.

Fixed during verification: `check_ram_logs()` now waits up to 12 s for the first
slow-log sample, because a freshly flashed board may still be inside the 10 s
cadence window when the checks start.

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
flash and checks in one step; set `HPN_UPLOAD_FS=1` to include the UI assets.

After boot, check:

1. `[config] loaded saved configuration from NVS` (no CRC/format failure) and
   `[l1] mixer opening limit 85%`.
2. Portal → Settings → Safety & timeouts shows **Maximum mixer opening** at
   85; saving 40 makes the Dashboard/Test page display 40 and the Test slider
   stop at 40.
3. Test Mode: commanding 80 % while the cap is 40 returns an error; a nudge
   upward past 40 stops at the cap.
4. Closing and end-stop homing still reach 0 %.

Always bound the serial monitor with `timeout`; never run an unbounded
`pio remote device monitor`. The automated suite covers checks 1–4 and the
bounded boot capture.

---

## 6. Working-tree state

Modified for this change:

```
firmware/README.md
firmware/HANDOVER.md
firmware/data/js/app.js
firmware/data/js/app.js.gz
firmware/include/hpn/config.h
firmware/src/config.cpp
firmware/src/config_store.cpp
firmware/src/l1_safety.cpp
firmware/src/l2_control.cpp
firmware/src/web_portal.cpp
plans/controller node/01_config_and_contracts.md
plans/controller node/03_safety_layer_l1.md
plans/controller node/04_control_and_coordination_l2.md
tests/runtime_check.py
tests/README.md
```

Untracked / unrelated and deliberately not part of this commit: `reviews/`,
the `simulation/sim_run_hil_*.csv/png` files, the `simulation/test_run_*`
artefacts, and the existing local edits under `simulation/`.

---

## 7. Open items

1. Confirm on a real HHN 1.0 carrier (`esp32s2`) that lowering the cap while
   the valve is open produces the expected close and that 85 % is mechanically
   identical to the old controller's ceiling.
2. Decide whether the dashboard should raise a visible warning when the
   estimated position is above the configured cap (for example after a config
   change or an encoder resync).
3. The simulator/HIL tooling in `simulation/` has local, unrelated edits that
   were not reviewed or committed in this session.
