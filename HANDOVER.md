# HeatpumpsN — project handover

**Status date:** 2026-10-27
**Repository:** `/home/gume/Work/HeatpumpsN`, branch `main`
**HEAD:** board-managed heater change set (`11e8dc9`, `bd2b3e1`, `689719a`,
`ed6b6a1`, `a31fb5a`, `df47eaa`)
**Firmware version string:** `0.3.0-web`

This is the entry point for whoever continues the project. It describes what
exists, what does not, how to build/flash/test, and what to do next. The
per-change engineering log lives in [`firmware/HANDOVER.md`](firmware/HANDOVER.md);
the design specifications live in [`plans/`](plans/).

---

## 1. TL;DR

* The controller firmware is **bench-complete** on the emulated carrier: it
  boots, runs a fixed FreeRTOS task set, drives the emulated HHN 1.0 carrier,
  serves a web portal, talks to MQTT/Home Assistant, and passes its runtime
  suite on a real ESP32 (`esp32dev_sim`; the exact check count is in
  `tests/README.md`). The current output-driver change set was flashed to that
  bench board (firmware + LittleFS) and passes the full suite:
  `89 passed, 0 failed, 0 skipped` with `--serial` and `82 passed` without it on
  2026-10-27.
* The **only flashed board so far is a plain ESP32 devkit running the emulated
  carrier**. The real ESP32-S2 + HomeNet Heatpumps 1.0 carrier has never been
  flashed or validated. That is the single largest open risk.
* Configuration is a **v3 CRC32-protected NVS blob** (1364 bytes) with automatic
  v1/v2→v3 migration. Adding a field requires bumping `kVersion` and extending
  the migration path — there is a `static_assert` that will stop you otherwise.
* The **plant watchdog is fed only by `T_io`**. AsyncTCP's own watchdog entry is
  disabled at build time (`-DCONFIG_ASYNC_TCP_USE_WDT=0`). Do not remove that
  flag; it caused a real bench reset.
* The largest functional gaps are **L3 adaptive tuning**, **hardware pump-current
  sampling**, **local encoder sampling**, **the remaining test sequences** and
  **unit tests**. Details in §7 and §8.

**If you read only three things:** this file, `firmware/README.md`, and
`plans/controller node/00_index_and_architecture.md`.

---

## 2. What the system is

A residential heating distribution controller with three hydraulic circuits and
one shared boiler/heater call:

| Circuit | Actuators | Temperature |
| --- | --- | --- |
| Radiator 1 (unmixed) | `pump_rad1` | boiler supply |
| Radiator 2 (unmixed) | `pump_rad2` | boiler supply |
| Floor (mixed) | `pump_floor` + 3-point motorized mixer | blended floor supply |

The ESP32 owns the real-time, safety-critical part: it arbitrates heater/pump
requests, derives the shared heater call from the three circuit demands, drives
the mixer to a target position, owns the per-circuit pump after-run, and
supervises limits. Home Assistant owns comfort logic and sends three per-circuit
heating-switch demands (`Heating loop 1`, `Heating loop 2`, `Floor heating`),
repeating each active `ON` as a keep-alive. There is no heater switch. The mixer
has **no absolute position sensor in normal operation**: position is
dead-reckoned from travel time, optionally corrected by an encoder reported over
MQTT (`pos_source = POS_SRC_HA_GENERIC`).

There is also an analysis workstream (old-system telemetry → controller
parameters, DuckDB) and a SysID/HIL simulation workstream. Both are secondary;
see §10.

---

## 3. Repository map

| Path | What it is | Tracked |
| --- | --- | --- |
| `HANDOVER.md` | **this file** — project-level entry point | yes |
| `README.md` | short project overview + quick start | yes |
| `AGENTS.md` | rules for AI/automation agents (remote agent, bounded serial) | yes |
| `firmware/` | PlatformIO project: controller firmware | yes |
| `firmware/HANDOVER.md` | engineering logs of recent change sets | yes |
| `firmware/README.md` | firmware details: targets, tasks, portal, NVS, safety notes | yes |
| `firmware/docs/hhn-1.0.md` | carrier wiring, output extender, MUX, current sensing | yes |
| `firmware/docs/io-emulation.md` | emulation model and its seams | yes |
| `firmware/data/` | LittleFS portal assets (`index.html`, `js/app.js`, `.gz`) | yes |
| `firmware/test/` | unit tests — **currently only a README** | yes |
| `tests/` | runtime integration checks against a live controller | yes |
| `plans/master_plan.md` | system architecture, actors, signal flow | yes |
| `plans/controller node/` | binding layer/contract specs (00–06 + web UI) | yes |
| `plans/observer_node.md`, `mqtt_messaging_plan.md`, `emulation_suite.md` | adjacent designs | yes |
| `analysis/` | DuckDB build, reports, `analysis/handover.md` | scripts only |
| `simulation/` | SysID/HIL Python workstream — **local, uncommitted edits** | partly |
| `reviews/` | one local-only review of an older commit — **untracked** | no |

Not tracked on purpose: `analysis/heatpumps.duckdb` (~264 MB), `analysis/db/`,
`others/` (third-party checkout with its own `.git`), `.pio/`, secrets.

---

## 4. Environment and access

### 4.1 The bench board

| Item | Value |
| --- | --- |
| MCU | ESP32-D0WDQ6 devkit (dual-core) |
| PlatformIO env | `esp32dev_sim` — boardless devkit, **emulated** HHN 1.0 carrier |
| Portal | `http://192.168.2.14` (may change after reflash/DHCP) |
| Wi-Fi | SSID `Testing`, weak signal (≈ −75 dBm) |
| MQTT broker | `smcl.tmit.bme.hu:31883`, anonymous in tests |
| Serial | `/dev/ttyUSB0` on the remote runner |
| Remote agent id | `EDI` |

The board does **not** drive real relays: `esp32dev_sim` compiles
`-DHPN_IO_EMULATION` and `-DHPN_BENCH_LOOPBACK`. It is a control-logic and
network bench, not a plant.

### 4.2 Remote agent workflow

The board is attached to a different machine. All build/flash/monitor operations
go through PlatformIO Remote. On the runner:

```bash
pio remote agent start
```

On your host, once:

```bash
pio account login          # or: pio remote login
pio remote agent list      # must show EDI (or whichever agent is used)
```

If the agent is not listed, the board is unreachable — stop and fix that before
debugging code. The agent must own `/dev/ttyUSB0`; close any other monitor.

### 4.3 Credentials and secrets

* `firmware/src/config.cpp` contains **bench-only compiled defaults**
  (`Testing` / `testing123`, empty MQTT host). These are first-boot fallbacks.
* Real Wi-Fi and MQTT credentials are entered in the portal and stored in NVS.
  **Never commit them.** Do not add a real SSID/password to `config.cpp`.
* The bench portal has no auth. Treat it as LAN-only. There is no captive portal
  provisioning flow yet.

### 4.4 If the board is gone

Nothing in the build or the non-`--serial` runtime suite needs the board except
the flash step itself:

```bash
pio run -d firmware -e esp32dev_sim     # local compile-only fallback
pio run -d firmware -e esp32s2          # compile the real target
```

Ask the owner to power the board, start the agent and log in before attempting
remote flash/monitor commands.

---

## 5. Build, flash, test

### 5.1 One-step path (recommended)

```bash
# from the repository root
tests/flash_and_check.sh                 # build + flash + 58 checks
tests/flash_and_check.sh --serial        # + reboot and boot-log checks (65)
HPN_UPLOAD_FS=1 tests/flash_and_check.sh # also refresh LittleFS assets
```

Environment variables: `HPN_ENV` (default `esp32dev_sim`), `HPN_URL` (default
`http://192.168.2.14`), `HPN_UPLOAD_FS=1`.

### 5.2 Manual path

From `firmware/`:

```bash
# application only
pio remote run --force-remote -d . -e esp32dev_sim --target upload

# portal assets changed? upload the filesystem too, before the app
pio remote run --force-remote -d . -e esp32dev_sim --target uploadfs
pio remote run --force-remote -d . -e esp32dev_sim --target upload

# checks
python3 ../tests/runtime_check.py --base-url http://192.168.2.14
python3 ../tests/runtime_check.py --base-url http://192.168.2.14 --serial
```

`--force-remote` makes the agent re-sync the project instead of reusing its
cached copy. Use it after editing sources or assets.

### 5.3 Serial monitor — hard rule

**Never run `pio remote device monitor` unbounded.** It opens a blocking stream
and freezes the session. Always bound it:

```bash
timeout 10s pio remote device monitor -b 115200 || true
```

The monitor needs a TTY; piped stdin fails. For scripted captures:

```bash
timeout 15s script -qec "pio remote device monitor -b 115200" /dev/null 2>&1 \
  | sed -r 's/\x1B\[[0-9;]*[mK]//g' | tr -d '\r'
```

`runtime_check.py --serial` already wraps this and **reboots the board**, so run
it last.

### 5.4 Build status at handover

| Env | Result | RAM | Flash |
| --- | --- | --- | --- |
| `esp32dev_sim` | SUCCESS | 25.2 % (82 460 B) | 77.1 % (1 263 476 B) |
| `esp32s2` | SUCCESS (compile only) | 28.9 % (94 764 B) | 76.0 % (1 245 531 B) |

Only a pre-existing `-Wformat` warning in `wifi_client.cpp`. Never treat a
compile-only `esp32s2` success as hardware validation.

### 5.5 Remote upload quirk (do not "fix" blindly)

PlatformIO Remote uploads with the `nobuild` target, which skips partition-table
parsing. `src/remote_upload.json` — a Python SConscript with a `.json`
extension, because Remote only syncs `platformio.ini`, `src/`, `include/`,
`lib/`, `data/` and binaries — injects the partition table and the `0x10000`
application offset. Consequences:

* remote uploads write only the app or the FS image;
* a board that has **never** been flashed (no bootloader/partition table) needs
  one local `pio run -t upload` first;
* keep the file inside `src/` and keep the `.json` extension.

### 5.6 Regenerating frontend assets

Editing `firmware/data/js/app.js` or `emulation.js` requires regenerating the
`.gz` sibling, otherwise the portal serves stale bytes:

```bash
gzip -9 -c firmware/data/js/app.js > firmware/data/js/app.js.gz
node --check firmware/data/js/app.js
```

`app.js.gz` at HEAD is verified byte-identical to its source after gunzip.

---

## 6. Architecture in ten minutes

### 6.1 Layers and pin ownership

```
HA / portal / MQTT
        │  *_intended
        ▼
  L4  comms    (T_comms, 50 ms, core 0)  ── wifi, mqtt, http, ws, leds
        │  (sets intents, serves API)
        ▼
  L2  control  (T_control, tick_ms=2000, core 1)  ── coordination, PI, mixer target
        │  *_requested, mix_target_pos, dhw_active
        ▼
  L1  safety   (T_safety, 500 ms, core 1)  ── limits, arbitration, mixer motor
        │  *_phys, mix_motion/mix_dir, alarms
        ▼
  L0  I/O      (T_io, 20 ms, core 1)  ── the ONLY layer that touches pins
        │  relays, expander, MUX, LEDs, 1-Wire
        ▼
   carrier (HHN 1.0 extender / PCF8574) / emulation model
```

Binding rules:

* **Only L0 touches hardware.** L1/L2 write logical state and call L0.
* **One writer for the output driver** (`T_io`). The selected 74HC595 or
  PCF8574 carries relay bits *and* MUX select lines; two writers would corrupt
  both.
* L1 always wins. L2 and the portal may clamp for coherence, but L1 re-checks
  every move and never opens past the effective ceiling.
* `T_io` feeds the plant task watchdog **only while** `T_safety`, `T_io` and
  `T_control` are inside their deadlines.

### 6.2 Tasks and timing

| Task | Period | Priority | Core (dual) | Feeds WDT |
| --- | --- | --- | --- | --- |
| `T_safety` | 500 ms | highest | 1 | no (supervised) |
| `T_io` | 20 ms | high | 1 | **yes, only feeder** |
| `T_control` | `config.tick_ms` (2000) | medium | 1 | no (supervised) |
| `T_comms` | 50 ms | low | 0 | no |
| `T_logger` | 1000 ms | lowest | 0 | no |

On single-core parts (`HPN_HAS_DUAL_CORE == 0`: S2, C3) all tasks are created
unpinned. The watchdog timeout is `max(tick_ms*4 + 1000, 5000)` → currently
**9000 ms**. Withheld feeds are logged at most once per second as events
**1900** ("feed withheld") / **1901** with the task ages, so a future reset is
diagnosable from the boot log.

### 6.3 The relay states

Every pump carries four booleans, and confusing them is the most common mistake
when reading the code:

* `*_intended` — the last accepted HA/Test Mode command (L4).
* `*_demand` — the effective heating demand after the per-circuit keep-alive
  (L2); a missed keep-alive drops it while `*_intended` stays set.
* `*_requested` — what L2's coordination and after-run decided (L2).
* `*_phys` — what L1 arbitrated and L0 actually drives (L1→L0).

The heater has no HA command: L2 derives `heater_intended = heater_requested`
from the three coordinated circuit requests. Test Mode may drive the heater
manually. MQTT switch topics publish the effective circuit demand; the four
binary sensors publish `*_phys`.

### 6.4 Configuration (v3)

* `struct Config` is **1364 bytes**, sectioned: provisioning, safety bounds,
  hardware constants, pin assignment, network/time, carrier wiring, MQTT
  meters, log streaming, output limits, pump calibration, mixer window, sensor
  roles, inferred DHW, output-driver/expander. See
  `firmware/include/hpn/config.h`.
* NVS blob = header (magic, version, size) + payload + **CRC32**.
* `kVersion = 3`, `kV1ConfigSize = 1288`, `kV2ConfigSize = 1348`. A v1 or v2
  blob is CRC-checked, copied over current defaults, sanitized, returned as
  `ConfigLoadResult::Migrated`, and **saved immediately** by `main.cpp` so
  migration happens once.
* `static_assert(sizeof(Config) == 1364)` — adding a field forces you to review
  the NVS format, bump the version and extend the migration path.
* `sanitize_config()` range-checks every field; an inconsistent pair (e.g.
  minimum above ceiling) is clamped, not rejected.
* A corrupt blob keeps compiled defaults, latches
  `WARN_NVS_CORRUPTED` / `ERR_NVS_DATA_CORRUPTED` (501) for the current boot,
  and clears only after a clean boot with all persisted blobs valid.
* Backup/restore: `GET /api/config/export`, `POST /api/config/import`. Do this
  before risky work. Relay counters (`hpnctr`) and anti-seize (`hpnanti`) have
  **no migration path** — an old-format blob of those is discarded.

### 6.5 Mixer model

* L2 writes only `state.mix_target_pos` (0…1). It never energizes the motor.
* L1 is a servo: it computes the error against the dead-reckoned position
  (`pos_est_dr`, advanced by `travel_time_ms`) and runs the motor for a bounded
  slice, stopping at the target.
* Position sources: dead reckoning always; `POS_SRC_I2C` exists in the enum but
  **is not implemented** — an external encoder arrives as `pos_encoder` over
  MQTT and is used for warnings/resync only.
* **Ceiling** = `min(mix_max_open_pct, dynamic_max)`, where the optional
  two-point dynamic maximum uses `mix_max_below_pct` / `mix_max_above_pct`
  around `mix_max_switch_c` with `mix_max_hysteresis_c`. Effective value is
  published as `state.mix_ceiling` / `circuits.floor.mixerMaxPct`.
* **Minimum** (`mix_min_open_pct`, default 40 %) applies **only** during normal
  floor-demand control. Closing, safety close, homing/sweep, DHW close, flush
  and the no-demand close all reach 0 %.
* Safety close (`mix_emergency`) latches and locks out other writers.
* Free-run (`mix_free_run_dir`) is for homing/sweep and ignores target+limits
  but has a hard deadline.
* Mixer anomalies are **warning-only**: motion longer than
  `travel_time_ms + 15 s` (`WARN_MIX_MOTION_OVERRUN`), and no encoder movement
  within 3 s of a commanded move when a valid encoder exists
  (`WARN_MIX_NO_RESPONSE`). Neither stops the motor — a seized valve, a dead
  encoder and a wiring fault look identical.

### 6.6 Safety hierarchy (L1)

Authoritative, in rough order of precedence:

1. **Hard over-temperature** — `T_mix_out ≥ t_max_hard` latches
   `ALARM_OVERTEMP_HARD` / 101, forces floor off and mixer closed. Clears only
   below `t_max_warn − t_overtemp_hysteresis`.
2. **High-temperature warning band** — `≥ t_max_warn` raises
   `WARN_OVERTEMP_HIGH` and blocks further opening.
3. **Gradient cutoff** — `dT/dt > grad_limit` raises `WARN_GRADIENT_CUTOFF`.
4. **Sensor postures** — mixer sensor loss is critical (201, floor off, mixer
   closed); return loss is a warning (202); boiler loss (203) blocks heating.
5. **Role plausibility** — only while heater + floor pump are physically on and
   all three readings are valid; requires
   `boiler ≥ forward ≥ return` within tolerance. 60 s bad → 603, 30 s good →
   clear. Otherwise "not measurable" (no false verdict).
6. **Heater lockout** — summer mode and anti-seize force the heater off.
7. **Anti-short-cycle backstop** (L1); per-circuit demand-loss (`301`) and pump
   after-run are L2-owned, so L1 has no separate heater/pump timeout backstop.
8. **Pump plausibility** — dry-run warning and welded-relay alarm from measured
   power, gated by `pump_power_check_enabled`.

DHW: `dhw_active = dhw_meter_active || dhw_inferred`. The meter is authoritative
and wins. Inference is opt-in and triggered by a boiler-temperature drop while
heating; a timeout latches 604 and must not re-latch on the same drop. While
DHW is active the mixer integrator freezes and the valve holds/closes safely.

### 6.7 L2 control loop

* PI on `(setpoint − predicted)` where prediction = measured + gradient ×
  `delay_s` (Smith-predictor style), gated by boiler supply margin, heater
  request and flush state; integrator anti-windup and leak.
* Floor warm-up flush with a ΔT-collapse pause.
* DHW priority freeze, long-pause detection, per-circuit demand keep-alive,
  derived heater call and board-owned pump after-run.
* Soft setpoint+Δ reaction: at `setpoint + mix_overshoot_delta_c` drive to
  `mix_overshoot_target_pct`, hold until `setpoint − mix_overshoot_recovery_c`,
  raise `WARN_MIX_OVERSHOOT`. Low priority; the L1 hard limit stays
  authoritative.
* Test Mode bypasses coordination (`intended → requested` directly) and keeps
  manual heater control, but L1 still arbitrates and L0 still drives.

### 6.8 Communications (L4)

* **Portal** on port 80, AsyncWebServer in the AsyncTCP task; WebSocket `/ws`
  pushes a state snapshot at 4 Hz. Every UI save is validated server-side and
  written to NVS only on submit.
* **JSON responses are pre-sized with `measureJson()`.** `AsyncResponseStream`
  otherwise grows one byte per `resizeAdd()`, which caused multi-thousand
  reallocations, HTTP timeouts and truncated ~10 KB log bodies. Keep this
  pattern for any new large response.
* **MQTT/HA discovery** under `homeassistant/...`; commands under `<base>/cmd/`,
  state under `<base>/state/`, availability retained with LWT. Log streaming is
  opt-in and non-retained.
* **Logs are RAM-only**: 96 event records, 180 slow samples (10 s cadence).
  Nothing is written to flash. Relay counters persist hourly in hardware builds
  only.
* **Status LEDs**: 5 addressable LEDs, mapped in `led_status.cpp`.
* **OTA** is available through the portal; use it with the same caution as a
  flash (see §9).

---

## 7. Implemented vs missing

### Implemented and bench-verified

| Area | Notes |
| --- | --- |
| L0 carrier + emulation | HHN 1.0 profile, runtime direct/74HC595/PCF8574 driver, expander bit map, polarity-aware safe state, LEDs, 1-Wire, MUX select |
| L1 supervision | hard/warn overtemp + hysteresis, gradient, sensor postures, role check, summer/anti-seize lockout, anti-short-cycle, pump plausibility |
| L1 mixer | continuous target-position servo, ceiling/minimum, dynamic max, emergency close, homing/sweep, warning-only anomalies |
| L2 control | PI + Smith predictor, flush + ΔT pause, DHW freeze/priority, long-pause, inferred DHW, overshoot reaction, per-circuit keep-alive, derived heater, board after-run |
| Config | v3 + v1/v2 migration + CRC + sanitize + export/import |
| Comms | Wi-Fi, MQTT/HA (incl. error sensors + log streaming), portal, WebSocket, OTA |
| Diagnostics | RAM event/slow/access logs, relay counters, 5 LEDs, boot-safe state |
| Bench hardening | watchdog ownership, pre-sized JSON, withheld-feed diagnostics |

### Missing, stubbed or unverified

| Gap | Where | Impact |
| --- | --- | --- |
| **Real HHN 1.0 / ESP32-S2 validation** | `esp32s2` env | highest risk: no real relay, extender, MUX or 1-Wire run yet |
| **Real PCF8574 / active-low relay validation** | runtime output driver | I²C backend and polarity are compile/emulation-verified only |
| **Pump-current hardware sampling** | `io_backend.cpp:268` TODO; `read_power_w()` returns 0 on hardware | pump protection cannot work on the real plant until done |
| **Local encoder sampling** | `l0_io.cpp:277`; `POS_SRC_I2C` declared but unimplemented | position stays dead-reckoned + external MQTT encoder |
| **L3 adaptive tuning** | not implemented; no `/config/tuning/set` | parameters stay manual/observer-owned |
| **Test sequence `heater-pump`** | `web_portal.cpp:2324` returns 501 | manual plant test not automatable (anti-seize, mixer-sweep and endstop-home exist) |
| **Unit/native tests** | `firmware/test/` has only README | regressions are caught only by the runtime suite |
| **Full L2 coordination edge cases** | `l2_control.h:10` says so | matrix is implemented but not exhaustively specified/tested |
| **2.0 carrier** | `HPN_CARRIER_BOARDLESS` only | pins, LEDs and power topology still TBD |
| **Captive-portal provisioning / portal auth** | portal is open on the LAN | credential entry is manual; no auth |
| **Counter/anti-seize migration** | old-format blobs discarded | acceptable now, document before a format change |
| **Secrets** | compiled bench credentials in `config.cpp` | replace with provisioning before deployment |

Stale comments that do **not** reflect current behaviour (update when you touch
the files): `io_backend.cpp:268`, `include/hpn/l1_safety.h:10`,
`include/hpn/l2_control.h:10`, and the `"arbitration_pending"` reason strings in
`web_portal.cpp:466` / `:2163`.

---

## 8. Prioritized next steps

### P0 — make the real plant safe to try

1. **Read `firmware/docs/hhn-1.0.md` end to end**, then verify the carrier wiring
   and the external over-temperature cutout / pump-cut switch. Those hardware
   protections are mandatory and independent of the firmware.
2. **Flash `esp32s2` with pumps and boiler disabled.** Watch the boot log for the
   `[l0] backend` line, extender init, 1-Wire discovery and relay safe state.
   Do not connect actuators on the first boot.
3. **Bench-test relays one at a time with a meter** before connecting pumps.
   The legacy extender pulls outputs to GND at reset and its relay board is
   active-high; a PCF8574 idles high and its relay board is active-low. Confirm
   the selected driver and `output_active_low` on the actual board before
   connecting actuators.
4. **Implement and calibrate pump current** (`io_backend.cpp:268`): sample the
   selected MUX channel through the carrier divider, then set
   `pump_current_offset_v` / `pump_current_w_per_v` with a clamp meter. Leave
   `pump_power_check_enabled` off until verified.

### P1 — close the functional gaps

5. **Local encoder support** (`POS_SRC_I2C`): pick the part (AS5600 is the
   plan's example), assign pins, implement sampling in L0, feed
   `pos_encoder`/`pos_encoder_valid`, and add staleness handling.
6. **Complete `heater-pump` test sequence** and align the portal's sequence
   buttons with what the firmware actually supports.
7. **Add native unit tests** for the pure logic: config sanitize/migration, mixer
   ceiling/minimum math, DHW inference state machine, role comparison, PI
   clamping. Keep them platform-independent so they run locally.
8. **Extend `tests/runtime_check.py`** for every new rule; it is the de-facto
   regression suite. The board-managed-heater change added discovery, keep-alive
   and after-run checks; run them on the bench before the next feature.

### P2 — better control

9. **L3 adaptive tuning** per `plans/controller node/` and the observer plan:
   implement `/config/tuning/set`, guard rails, and persisted tuning state.
10. **Review defaults against the real hydraulics**: 40 % minimum, 85 % absolute
    ceiling, dynamic switch point, `travel_time_ms`, `delay_s`, PI gains,
    flush duration, overshoot delta/target.
11. **Decide escalation policy for warning-only mixer anomalies** once a
    reliable encoder exists (stop vs. warn).
12. **Full L2 coordination matrix**: treat the specification as the source of
    truth and add tests for each cell.

### P3 — housekeeping

13. Replace compiled bench credentials with a provisioning flow; add portal
    auth if it will ever be reachable beyond the LAN.
14. Add migration paths (or explicit discard policy) for counters and anti-seize
    before changing their formats.
15. Refresh the stale comments listed at the end of §7.
16. Decide what to do with the analysis and simulation workstreams (§10).

---

## 9. Gotchas and invariants

1. **Never feed the watchdog from network code.** Only `T_io` feeds it. Keep
   `-DCONFIG_ASYNC_TCP_USE_WDT=0` in every environment. A long HTTP callback
   must never be able to reset the plant.
2. **Pre-size large JSON responses.** Use `measureJson()`/`send_json()`; do not
   let `AsyncResponseStream` grow byte-by-byte.
3. **Never run the serial monitor unbounded.** Always `timeout`.
4. **Upload the filesystem before the app** when assets changed, then let the
   board reset. Stale `.gz` files silently serve old JS.
5. **The output driver has a single writer (`T_io`).** It carries relay bits
   and MUX select lines. The safe byte is polarity-dependent: `0x00` for the
   legacy active-high carrier, `0xFF` for an active-low PCF8574 board.
6. **`*_intended` ≠ `*_demand` ≠ `*_requested` ≠ `*_phys`.** Debug the correct
   stage. A stale keep-alive clears the demand but not the intent.
7. **Minimum opening never blocks a close.** If you add a path that closes the
   mixer, do not route it through the minimum.
8. **L1 is authoritative.** Do not clamp only in the UI; always re-check in L1.
9. **The dynamic maximum is opt-in.** Defaults exist but `mix_max_dynamic` is
   false.
10. **Sensor-role verification requires active heating.** An idle plant cannot
    prove the temperature order; "not measurable" is correct, not a bug.
11. **Inferred DHW must not override a meter.** `dhw_active` is an OR, and the
    meter side wins by construction.
12. **Config struct changes need a version bump and migration.** The
    `static_assert` is there to stop you. Preserve the v1 prefix behaviour.
13. **A `501` latch clears only on the next clean boot.** Saving settings does
    not clear the live flag; that is deliberate.
14. **Emulation is not hardware.** `HPN_BENCH_LOOPBACK` and `HPN_IO_EMULATION`
    are never compiled into `esp32s2`. A green `esp32dev_sim` run proves logic,
    not wiring.
15. **Flashing replaces the running plant firmware.** Do it only with pumps and
    boiler disabled or with explicit intent. OTA carries the same risk.
16. **Single-core parts** (S2, C3): never pin to Core 1; the task set handles it
    via `HPN_HAS_DUAL_CORE`, but new code must not assume two cores.
17. **Runtime checks mutate live settings** and restore them. They are safe on
    the bench; read `tests/README.md` §"Side effects" before running them on a
    plant.
18. **`--serial` reboots the board.** Run it last.

---

## 10. Uncommitted local work

At this handover the working tree is **clean**. The board-managed-heater change
set is committed in small steps: `11e8dc9` (core firmware), `bd2b3e1` (portal),
`689719a` (runtime tests), `ed6b6a1` (version bump), `a31fb5a` (docs/plans) and
`df47eaa` (after-run test timing). There are no unrelated local edits in
`simulation/` or untracked review files; the earlier SysID/HIL workspace was
either committed or cleaned up separately.

**Analysis workstream:** `analysis/handover.md` explains the old-system
telemetry, DuckDB build (`build_duckdb.py`), data conventions and derived
parameters. `analysis/heatpumps.duckdb` and `analysis/db/` are local and
untracked.

---

## 11. First-week checklist

- [ ] `pio remote agent list` shows the agent; `pio account login` done.
- [ ] `curl http://192.168.2.14/api/status` returns `ok: true`.
- [ ] `pio run -d firmware -e esp32dev_sim` and `-e esp32s2` both succeed.
- [ ] `python3 tests/runtime_check.py` → all checks pass on the current bench build.
- [ ] `tests/flash_and_check.sh --serial` → all checks pass, no reset, no `501`.
- [ ] Boot log shows `[l1] mixer opening limit …`, dynamic-max and minimum lines,
      plus `[rtos] task WDT armed at 9000ms`.
- [ ] `GET /api/config/export` saved as a backup.
- [ ] Read `plans/master_plan.md` and `plans/controller node/00…04`.
- [ ] Read `firmware/docs/hhn-1.0.md` before touching the real carrier.
- [ ] Confirm with the owner who may operate the plant and when.
- [ ] Agree on the P0/P1 order in §8 before starting.

---

## 12. Where to read more

| Question | Document |
| --- | --- |
| What changed in the last session, in detail? | `firmware/HANDOVER.md` |
| How do targets, tasks, portal and NVS work? | `firmware/README.md` |
| How is the 1.0 carrier wired? | `firmware/docs/hhn-1.0.md` |
| How does the emulation work and what can I fake? | `firmware/docs/io-emulation.md` |
| What do the runtime checks cover? | `tests/README.md` |
| System architecture and actors | `plans/master_plan.md` |
| Layer model, task map, watchdog, fail matrix | `plans/controller node/00_index_and_architecture.md` |
| Config/state contracts and enums/codes | `plans/controller node/01_config_and_contracts.md` |
| Hardware and L0 duties | `plans/controller node/02_hardware_and_l0.md` |
| L1 safety contracts | `plans/controller node/03_safety_layer_l1.md` |
| L2 algorithms and coordination | `plans/controller node/04_control_and_coordination_l2.md` |
| Modes, Test Mode, sequences | `plans/controller node/05_modes_and_testing.md` |
| MQTT topics, logs, HA interface | `plans/controller node/06_interfaces_and_logging.md` |
| Portal UX and API | `plans/controller node/web_interface.md` |
| Old-system data and analysis conventions | `analysis/handover.md` |
