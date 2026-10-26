# Firmware session handover — mixer movement, crash visibility, logging, CPU load

**Date:** 2026-10-26
**Base commit:** `212dd3a` ("Fix MQTT stack overflow and add anti-seize, CPU load and hardware I/O")
**Working tree:** uncommitted changes (see §5). Nothing has been committed from this session yet.
**Device:** remote ESP32 (`esp32dev_sim` emulation build), portal `http://localhost:9090`, Wi-Fi `Testing`, controller `192.168.2.14`.

> **Read this first.** The board was re-flashed in this follow-up session with the
> current tree (firmware **and** filesystem) over the `EDI` remote agent on
> `/dev/ttyUSB0`; it boots clean and connects to Wi-Fi/MQTT. The earlier
> intermediate CPU-load probe build is gone. The functional mixer check still has
> to be done from a host on the controller's network.

---

## 1. Objective of this session

Fix six problems reported after the previous session:

1. A crash occurred but there was no serial monitor attached, so the cause was not visible.
2. When the mixed temperature is at setpoint, the mixer still opens.
3. On over-temperature, while the mixer closes, the motor does stop–start–stop–start very frequently, and keeps doing it even when the position value is 0.
4. Altogether there is an enormous amount of motor movement.
5. Console logging seems very slow.
6. CPU load always reads 0 %.

---

## 2. Root causes and fixes

### 2.1 Mixer opens while at setpoint (issue 2)

`l2::service_mixer_control()` (in `firmware/src/l2_control.cpp`) computed an
**absolute** target position from the feedforward model:

```
target = pos_ff + kp * error + integrator
```

At setpoint `error ≈ 0`, so `target ≈ pos_ff`. Because the model is imperfect,
`pos_ff` generally differs from the current valve position, so the loop
commanded a move to an unrelated absolute position every control tick. That is
the "it still opens when at setpoint" behaviour.

**Fix:** when the predicted error is inside `deadband_temp`, **hold the current
position** (`mix_target_pos = pos_est_dr`) instead of seeking `pos_ff`. The
feedforward is now only used when the error is meaningful. Also clamp the target
change per control tick to `max_step = 0.10` (10 %) so a large feedforward
correction cannot produce a long motor burst.

The hold is skipped during the warm-up flush (`flush_active`), because then the
position must still be clamped to `config.flush_max_pos`.

### 2.2 Over-temperature close stop/starts, and keeps going at 0 (issue 3)

Two defects in `l1::tick()` / `l1::service_mixer()` (`firmware/src/l1_safety.cpp`):

* The force-close path re-issued a `max_pulse_ms` (1000 ms) pulse on **every**
  L1 tick. At the closed soft limit the pulse immediately clamped to 0 and
  stopped, then restarted on the next tick → ~1 Hz stop/start.
* Any move longer than one second was chopped into 1 s bursts separated by
  `pulse_cooldown_ms`, so closing from mid-travel produced dozens of
  stop/starts.

**Fixes:**

* `service_mixer()` now refuses to start a pulse that would immediately hit a
  soft limit:
  `dir < 0 && pos_est_dr <= 0` or `dir > 0 && pos_est_dr >= 1` returns without
  starting. This stops the "restart at 0" loop.
* The force-close path now drives **one continuous move** toward the closed end
  stop (`ms = travel_time_ms`), and `service_mixer()`'s soft limit stops the
  motor when `pos_est_dr` reaches 0. The worst-case drive time is bounded by
  `travel_time_ms`.
* An in-flight **opening** pulse is aborted immediately when force-close
  engages, so an over-temperature fault never finishes an open move.

Note: continuous close does not increase total motor-on time versus the old
pulsing scheme — `service_mixer()` integrates `pos_est_dr` and stops at the soft
limit, so both schemes drive for roughly `pos_est_dr * travel_time_ms`. It only
removes the stop/start transients.

### 2.3 Mixer redesigned as a pure target-position servo (issue 4, follow-up)

The first fix still kept the L2→L1 pulse-request plumbing (`mix_pulse_ms_req`,
`max_pulse_ms`, `pulse_cooldown_ms`). Per the follow-up review the mixer is now a
**pure target-position servo**: every layer only sets `mix_target_pos`, and L1 is
the single place that energizes the motor.

* **No pulses, no max length, no cooldown.** `min_pulse_ms`, `max_pulse_ms` and
  `pulse_cooldown_ms` are removed from the configuration (web UI, config JSON and
  validation). The `Config` struct keeps them as *reserved* fields purely so
  saved-NVS blobs keep their binary layout (see `config.h`); nothing reads them.
* **`l1::service_mixer()`** (firmware/src/l1_safety.cpp) drives continuously until
  the dead-reckoned position reaches the target, a soft end stop (0 %/100 %) or
  the free-run deadline. State is now `mix_motion` + `mix_dir` instead of
  `mix_pulse_*`.
* **Immediate reversal with a tiny physical delay.** When the target jumps to the
  other side of the current position (e.g. opening toward 40 %, target suddenly
  20 %), L1 stops, waits `kMixerDirSwitchMs` (50 ms) for the relay contacts to
  drop, switches direction and starts again. There is no cooldown anywhere.
* **Manual nudges are targets.** `/api/test` mixer `open`/`close` steps the target
  by **±2 %** (no pulse length); `stop` freezes the target at the current
  position and interrupts a move in progress.
* **Emergency is not a special movement.** Over-temperature / sensor-fault /
  welded-relay handling simply latches `mix_emergency`, sets the target to the
  safe position (0 %) and **locks every other writer out** while held: L2's
  closed loop returns early and the web test commands answer 409. The move itself
  is the ordinary servo stroke to 0 %.
* **Free-run (homing/sweep)** became `mix_free_run_dir` + `mix_free_run_until_ms`
  (direction + deadline) instead of a boolean plus a pulse request; L1
  self-heals an expired or unbounded free-run request.
* `l2::service_mixer_target()` and the pulse arbitration in `l1::tick()` are
  deleted; `mqtt` `mixer_state`, the portal counters and `/api/sim` use
  `mix_motion`/`mix_dir`.

Total motor-on time is unchanged versus the chopped scheme; only the stop/start
transients are gone, and direction changes now cost one 50 ms gap.

### 2.3a Emulation & dashboard polish (same session)

* All three emulation temperature inputs now use one scale, **15–85 °C**
  (`emulation.js` sliders and the `/api/sim/temperature` server-side range).
* The dashboard Mixer card shows **Target position** and the live **Command**
  (`idle` / `opening` / `closing` / `emergency close`) next to the estimated
  position; the Test-mode mixer card got the same rows and ±2 % nudge buttons.
  New live-state keys: `floor.mixerMotion`, `floor.mixerDir`,
  `floor.mixerEmergency`.

### 2.4 Crash with no monitor attached (issue 1)

There is no way to retrieve a past panic without a monitor attached in this
build. What was added:

* `firmware/src/main.cpp` logs `esp_reset_reason()` by name at boot.
* `firmware/src/web_portal.cpp` adds `reset_reason_name()` and:
  * publishes `resetReason` in `/api/status`;
  * appends a boot event to the in-RAM event log (`code 1004`, severity `error`
    for panic / watchdog / brownout, otherwise `info`).
* The portal System page shows a new **Last reset** row.

So after the next unexpected reboot the cause is visible in the portal without a
serial monitor. **Not done:** no core-dump-to-flash capture and no retrieval
tooling. If richer post-mortem is needed, enable
`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH` (partition `coredump` already exists at
`0x3f0000/0x10000`) and read it with `esp-coredump` / `esptool`; that needs
sdkconfig work because the Arduino framework ships a prebuilt FreeRTOS.

### 2.5 Slow console logging (issue 5)

The emulation page polls `/api/sim` on a timer and every request was written to
the in-RAM access log **and** echoed to the serial console.

* `log_access_request()` now skips `/api/sim` (kept in the access log? no —
  skipped entirely).
* Emulation page poll interval raised 750 ms → 1500 ms
  (`firmware/data/js/emulation.js`).

The main SPA is not a significant contributor (status every 30 s, events every
10 s, counters every 30 s, live state over the WebSocket). If the console is
still noisy, look at `Serial.printf` inside `l1::apply_relay` (fires on every
relay transition) — with the movement fixes there are far fewer transitions.

### 2.6 CPU load always 0 % (issue 6) — **UNRESOLVED, see §6.4**

The previous code computed load from FreeRTOS run-time counters
(`uxTaskGetSystemState`, idle-task `ulRunTimeCounter`). It reads 0.00 % because
the counters report idle ≈ total. A windowed (1 s delta) version behaves the
same. Raw counter deltas are exported as `cpuTotalDelta` / `cpuIdleDelta` in
`/api/status` so the next session can tell whether the counters are advancing
and how `idle` relates to `total`.

A low-priority probe-task alternative was implemented and flashed for one test:
it reported ~75 % load on Core 1, which contradicts "always 0", but it was
noisy and is **not** in the tree. See §6.4 for the full experiment notes.

---

## 3. Files changed in this session

| File | Change |
| --- | --- |
| `firmware/src/l2_control.cpp` | Hold-at-setpoint when `|error| <= deadband_temp` (skipped during flush); `max_step = 0.10` per-tick target clamp; `integrator_val` published before the early return. `service_mixer_target()` now issues **one continuous stroke** toward the target (`mix_pulse_ms_req = travel_time_ms`) instead of `max_pulse_ms` bursts, with the cooldown armed on the pulse falling edge. |
| `firmware/src/l1_safety.cpp` | `service_mixer()` soft-limit pre-check; **stops on arrival at `mix_target_pos`** and refuses to start a move already at/past the target; force-close drives one continuous move (`travel_time_ms`) and aborts an in-flight opening pulse; opening is also aborted when `allow_mixer_open` drops (high-temp / gradient / boiler fault). |
| `firmware/src/main.cpp` | `#include <esp_system.h>`; `reset_reason_name()`; boot log line `[boot] reset reason: ...`. |
| `firmware/src/web_portal.cpp` | `#include <esp_system.h>`; `reset_reason_name()`; `resetReason`, `cpuTotalDelta`, `cpuIdleDelta` in `/api/status` (+ raw deltas); boot event with reset reason; skip `/api/sim` in the access log; windowed CPU-load sampler restored; CPU globals initialised in `begin()`. |
| `firmware/data/js/app.js` | CPU load shown with 2 decimals; new **Last reset** row on the System page. |
| `firmware/data/js/emulation.js` | Poll interval 750 ms → 1500 ms. |
| `firmware/data/js/app.js.gz`, `firmware/data/js/emulation.js.gz` | Regenerated. |

No changes to `state.h`, `config.h`, NVS schema, tasks or partitions in this
session. The probe-task edits to `tasks.cpp` / `tasks.h` were fully reverted.

---

## 4. Verification performed this session

* Both environments build clean: `pio run -e esp32dev_sim` and
  `pio run -e esp32dev` (app flash ≈ 76 % of 1.6 MiB).
* `node --check` passes for `app.js` and `emulation.js`; `.gz` siblings
  regenerated before the filesystem upload.
* **Mixer hold:** with boiler 65 / return 25 / mix 35 (setpoint 35) and floor +
  heater intent, after the flush the mixer reported `pos 0, target 0` over
  several samples — no movement at setpoint.
* **Over-temperature close:** raising mix to 55 left `motion False`, `dir 0`,
  `est 0` over repeated samples — no stop/start loop at the end stop.
* **Reset visibility:** after the flash the portal `/api/status` returned
  `resetReason: "power-on"` and the boot event appeared in the event log.
* **CPU probe experiment (since reverted):** the probe task version reported
  `cpu ≈ 75 %` with occasional `≈ 0.9 %` readings, `taskCount 17`. This was too
  noisy to ship.
* **Servo redesign (this follow-up):** both environments build clean again after
  the redesign; `node --check` passes for `app.js` and `emulation.js`. The
  earlier continuous-stroke intermediate build was flashed to the remote ESP32
  (`/dev/ttyUSB0` on agent `EDI`) and booted clean (Wi-Fi + MQTT connected).
  **The servo redesign still needs a flash + bench check:** drive a target (e.g.
  45 %) and expect one continuous motor-on period, an immediate stop→reverse
  when the target crosses the current position, and `mixer_state` showing
  `opening`/`closing` for the whole stroke.
* **Not verified:** MQTT (user tested independently; connection was failing
  during the session), real hardware, and the final-tree device build.

---

## 5. Exact working-tree state

Uncommitted (no commit made this session):

```
 M firmware/data/js/app.js
 M firmware/data/js/app.js.gz
 M firmware/data/js/emulation.js
 M firmware/data/js/emulation.js.gz
 M firmware/src/l1_safety.cpp
 M firmware/src/l2_control.cpp
 M firmware/src/main.cpp
 M firmware/src/web_portal.cpp
```

**Decision needed at the start of the next session:** commit these fixes
(they are self-contained and build clean) before doing more work.

---

## 6. Open items / next steps

### 6.1 Re-flash the final build — **DONE (this follow-up)**
Firmware + filesystem were flashed to the remote ESP32 over agent `EDI`. The
auto-detected port (`/dev/ttyS0`) is wrong/busy; the CP2102 device is on
`/dev/ttyUSB0`, so the port must be specified explicitly:

```bash
cd firmware
cd data && for f in index.html emulation.html favicon.svg css/style.css js/app.js js/emulation.js; do gzip -9 -c "$f" > "$f.gz"; done && cd ..
pio remote run --force-remote -d . --target uploadfs --upload-port /dev/ttyUSB0
pio remote run --force-remote -d . --target upload --upload-port /dev/ttyUSB0
timeout 12s script -qec "pio remote device monitor -b 115200 --port /dev/ttyUSB0" /dev/null || true
```

`pio remote device monitor` refuses to run without a TTY, so wrap it in
`script -qec` (still under `timeout`).

### 6.2 CPU load (issue 6) — diagnose with the raw deltas
After re-flashing, read `/api/status` and compare `cpuTotalDelta`,
`cpuIdleDelta`, and `cpuLoad`.

* If `cpuTotalDelta == 0`, the run-time counters are not advancing:
  `configGENERATE_RUN_TIME_STATS` / trace facility are effectively off in the
  prebuilt FreeRTOS. Options: (a) show `n/a` instead of a misleading `0.00 %`;
  (b) enable run-time stats in sdkconfig (needs framework/sdkconfig work);
  (c) measure per-core idle differently.
* If `cpuIdleDelta ≈ cpuTotalDelta` always, the counter only reflects idle /
  one core. Note `uxTaskGetSystemState` **is** linked, so the trace facility is
  compiled in — verify the counter unit (ESP-IDF uses `esp_timer_get_time()`,
  microseconds) and whether dual-core totals are being attributed correctly.
* The probe-task experiment (§4) suggested Core 1 genuinely may be busy
  (~75 %), which would be a real find worth investigating (look at `T_io` /
  `l0::tick` duration). The probe approach itself was noisy under
  `taskYIELD()`-based scheduling and was reverted; if revisited, prefer a
  monotonic idle counter instead of a yield loop.

### 6.3 Normal-move granularity
If the user still sees too many stop/starts during ordinary (non-emergency)
moves, address the `max_pulse_ms` granularity discussion in §2.3.

### 6.4 Remove the temporary CPU debug fields
`cpuTotalDelta` / `cpuIdleDelta` are diagnostic only. Once the load is trusted,
remove them or move them behind a debug flag.

### 6.5 MQTT
The broker link was failing during this session (Pinggy tunnel likely expired
again). The user is testing this independently. Broker host used last time:
`xhryj-152-66-244-175.run.pinggy-free.link:33065`; the tunnel URL changes when
it expires. The stack-overflow fix from `212dd3a` (4096-byte static payload
buffer, static parser scratch buffers, `t_comms` stack 8192) is in place.

### 6.6 Previously known, still open (from the prior handover)
* Hardware-only validation: Dallas temperature reads, AC current calibration,
  relay polarity (needs the physical HHN 1.0 board).
* Observer/L3 parameter push, real encoder/endstop, logger persistence, and the
  heater+pump Test Mode sequence (still returns 501).
* Confirm the user's retained HA discovery configs for `mixermeter` and
  `ems-esp` are intact (test retained topics published earlier were cleared).

---

## 7. Quick reference

* Build/flash:
  * `pio run -e esp32dev_sim` (default env, emulated HHN 1.0 — what the board runs)
  * `pio run -e esp32dev` (boardless hardware profile)
  * `pio remote run --force-remote -d . --target uploadfs`
  * `pio remote run --force-remote -d . --target upload`
* Monitor (always bound it):
  `timeout 15s script -qec 'pio remote device monitor -b 115200' /dev/null`
* Regenerate tracked gzips before `uploadfs` (see §6.1).
* Device: `192.168.2.14`, tunnel `http://localhost:9090`, Wi-Fi `Testing` /
  `testing123`, firmware string `0.2.0-web`.
* Partitions: nvs `0x9000/0x5000`, otadata `0xe000/0x2000`,
  app0 `0x10000/0x190000`, app1 `0x1a0000/0x190000`, spiffs `0x330000/0xc0000`,
  coredump `0x3f0000/0x10000`. Keep `firmware/partitions.csv` and
  `firmware/src/partitions.csv` identical.
* Saved mixer config at last check: `mixer_wiring=0`, `travel_time_ms=145000`,
  `mix_ff_offset=6.75`, `demand_timeout_s=600`, `grad_limit=0.3`,
  `summer_mode=false`, `anti_seize_interval_s=2592000`.
* Key tunables for this work: `deadband_temp` (2.0 K hold band),
  `deadband_pos` (0.02), `max_pulse_ms` (1000), `pulse_cooldown_ms` (200),
  `travel_time_ms` (145000), `recalc_threshold` (0.12).
