# HeatpumpsN controller firmware — handover

Entry point for continuing the controller work (firmware, portal, floor
simulator, Home Assistant side) in a new session. Updated 2026-11-09.
Open work is in [`ISSUES.md`](ISSUES.md). The observer has its own handover:
[`../observer/HANDOVER.md`](../observer/HANDOVER.md); the old-system data
analysis too: [`../analysis/handover.md`](../analysis/handover.md).

## 0. Working agreements (read first)

The owner's standing instructions; they override defaults.

1. **No firmware or filesystem upload to the live S2 without an explicit
   "go"** from the owner for that specific upload. Build, test (host tests,
   simulator), report, then wait. A previous "go" does not carry over.
2. **The bench ESP32 (`localhost:30080`) is switched off** and stays off unless
   really necessary. If an update is risky (boot path, partitions, OTA, memory
   layout, anything that could stop the S2 from booting or answering), say so
   and ask for the bench instead of only asking for "go".
3. **No credentials in the repository**, including the ESPHome OTA password.
   Operational data of the installation stays out of git.
4. Commit with the attribution line; **do not push** unless asked.
5. Never run a serial monitor unbounded (`../AGENTS.md`).

## 1. What the system is

A residential heating distribution controller with three hydraulic circuits and
one shared boiler/heater call:

| Circuit | Actuators | Temperature |
| --- | --- | --- |
| Radiator 1 (unmixed) = MF, middle floor | `pump_rad1` (pump 1) | boiler supply |
| Radiator 2 (unmixed) = TF, top floor | `pump_rad2` (pump 2) | boiler supply |
| Floor (mixed) = BF, bottom floor | `pump_floor` (pump 3) + 3-point motorised mixer | blended floor supply |

The ESP32 owns the real-time and safety-critical part: it arbitrates heater and
pump requests, derives the shared heater call from the three circuit demands,
drives the mixer, owns the per-circuit pump after-run and supervises limits.
Home Assistant owns comfort logic and the floor setpoint, and sends three
per-circuit heating-switch demands, repeating each active `ON` as a keep-alive;
there is no heater switch. The mixer has **no absolute position sensor**:
position is dead-reckoned from travel time; a MixerMeter (BMI160 encoder) on
the valve reports over MQTT and is cross-checked.

Around it: a gas condensing boiler read by **EMS-ESP**, room sensors in Home
Assistant, Zigbee2MQTT devices, and the **observer** (`../observer/HANDOVER.md`), which records and
analyses everything but never commands the plant.

## 2. Where things stand

| Area | State | Details |
|---|---|---|
| Controller firmware 0.4 (`firmware/`) | Running on the live LOLIN S2 mini (HomeNet v0.1) | `README.md` |
| Predictive mixer control (MPC) | Running; supervised fallback to the legacy PI and homing; 0.3–0.8 s per plan on the S2 | `docs/predictive-mixer.md` |
| Security (HPN1) | Challenge-response request signing, users `admin`/`monitor`, signed uploads, sealed secrets, signed MQTT commands | `docs/security.md` |
| Partition layout / ESPHome migration | Done on the live S2 | `docs/partition-migration.md` |
| HIL floor simulator (`floor_simulator/`) | Plant, boiler, bridges, dashboard, tuning tools; runs the firmware MPC and L2 code on the host in its tests | `../floor_simulator/README.md` |
| Home Assistant side | Keep-alive blueprint in the repo, to be installed | `../homeassistant/README.md` |
| HomeNet v1.x board | Firmware profile exists; hardware not built | `docs/boards.md` |

### 2.1 The live S2 (plant controller)

Reached through a forward at `http://localhost:30081` (when the owner has it
up). Firmware and portal files at commit `9fcb58c` (setpoint 15–50 °C,
mixer relay runtime).

On the S2 since 2026-11-01:

* every boot **homes the mixer** (full close stroke, position 0); an encoder
  that disagrees afterwards is distrusted (never used to resync) until it
  agrees again;
* the MixerMeter encoder is received (ESPHome's `stat_t` discovery key);
* HA's floor setpoint command is **retained** (HA owns it; the broker hands it
  back after reboots) and saved as the fallback after a minute; the owner
  should set the setpoint once in HA so a retained command exists;
* **Diagnostics → MQTT inputs**: last encoder, DHW and setpoint messages;
* MPC: no opening guards while the floor forward is > 5 K below the setpoint
  (faster, gentler heating starts); floor ΔT-collapse pause only within 2 K of
  the setpoint;
* floor setpoint limited to 15–50 °C in 0.5 steps; mixer relays count runtime;
* LED driver fixed, board LED order, radiators green; Test Mode LED test;
* watchdog health-check race fixed; HA Mixer position (%) and Wi-Fi diagnostics.

Upgrade checklist (`docs/esphome-upgrade.md`):

- [x] 1–4: ESPHome OTA, maintenance page, partition migration, portal files
- [x] relays and LEDs checked; MQTT connected to `mqtt.home`; temperatures plausible; heating runs
- [ ] 5: change the factory passwords (still admin/admin; tools below assume
      them). **Owner's decision 2026-11-09:** deferred until development ends;
      the site's network access is restricted in the meantime.
- [ ] 6.2: verify the sensor roles while heating
- [ ] 7: relay counter preset (`esp32s2_counter_preset`, ESPHome values 1:1)
- [ ] 8: configuration export as backup

Facts about this board:

- It keeps **ESPHome's bootloader**, which supports rollback. A new image is
  confirmed after 2 min with Wi-Fi, or at once before an admin-requested
  reboot (so firmware and portal-file uploads can follow each other directly).
- Wi-Fi `Korona-IoT`, IP 192.168.2.74; fallback AP `HeatpumpsN-Setup` after
  2 min without a connection (captive portal at 192.168.4.1).
- The S2 has no FPU and one core; internal RAM is the limit. PSRAM takes
  ordinary allocations from 512 B, the logs and the state frames. Watch
  `freeHeap`, `minFreeHeap`, `largestFreeBlock` in `/api/status`
  (last seen: ~41 KB free, low-water mark ~35 KB).
- Controller log streaming to MQTT is on, so the observer records its events.

### 2.2 Pending upload (2026-11-09), not on the S2 yet

Built from `7eb5eaa` (firmware) and the portal files; **no upload has been
made**. The owner will start it; an OTA firmware + filesystem upload, or a
parameter-only push, waits for an explicit go.

* **MPC retry handling:**
  * a failed automatic retry no longer blocks MPC for good: the same anomaly
    is counted, a different one starts a new count, and the third failure
    within the 2 h probation blocks it (event 1340 carries the count);
  * a retry that survives the probation clears the count;
  * cautious stable run 30 -> **20 min**; the log check behind it is in
    `docs/predictive-mixer.md` §3 (6 of 8 overshoots 3.5-4.9 min after the
    floor pump start; two late, one 87 s after a retry);
  * status publishes `retryBlocked`, `retryStrikes`, `retryStrikeLimit`.
* **Portal:** a permanent warning banner on the dashboard while retries are
  blocked (not dismissible) and an **Unblock and resume MPC** button
  (`POST /api/mpc/unblock`, event 1343). The mixer card shows the counter.
* **Images:** `pio run -e esp32s2` and `-t buildfs`; `data/js/app.js.gz` was
  regenerated (the filesystem carries only the gzip copy). Latest builds:
  RAM 23.5 %, flash 75.0 %.
* **Upload checklist when the owner says go:**
  1. pumps/boiler in a safe state, or explicit intent (OTA carries the same
     risk as flashing; rollback is available);
  2. `HPN_PORTAL_PASSWORD=admin python3 tools/http_ota.py
     .pio/build/esp32s2/firmware.bin localhost:30081`;
  3. the same with `.pio/build/esp32s2/littlefs.bin` for the portal (the
     admin-requested reboot confirms the new image without waiting 2 min);
  4. check `/api/status` for `retryBlocked`/`retryStrikes` and the dashboard
     banner/button; the lights and mixer state as after any flash.
* **Parameter-only path:** the observer sends approved sets to
  `cmd/mpc/params`; it is off (`parameters.push_enabled`) and no set is
  approved. The 2026-11-09 re-identification says the current mixer
  parameters stay (backtest not recommended), so there is nothing to push
  yet.

## 3. Repository map (controller side)

| Path | What it is |
| --- | --- |
| `firmware/` | PlatformIO project: controller firmware, portal assets (`data/`), tools (`tools/`), host test harness (`test/host/`) |
| `firmware/docs/` | boards, I/O emulation, predictive mixer, security, partition migration, ESPHome upgrade checklist |
| `firmware/test/live/` | live checks against a running controller (HPN1 security) |
| `floor_simulator/` | HIL simulator: plant, boiler, bridges, dashboard, tuning tools, tests (incl. firmware MPC and L2 on the host) |
| `homeassistant/` | Home Assistant files: heating keep-alive blueprint |
| `plans/` | system architecture, controller-node layer specs, MQTT design, schematics |
| `tests/` | older runtime integration checks against a live controller |

Not tracked on purpose: `.pio/`, `firmware/include/hpn/credentials_local.h`, `others/`.

## 4. Working on it

### 4.1 Access from the development machine

| What | Where |
|---|---|
| Live S2 (when the owner forwards it) | `http://localhost:30081` (admin/admin until the passwords are changed) |
| Bench controller | `http://localhost:30080` (switched off, see §0.2) |
| MQTT broker (development; home data under `mirror/`) | `localhost:31883` |
| Observer InfluxDB (recorded plant data, read-only use) | see `../observer/HANDOVER.md` |
| Serial, flashing over USB | `pio remote …` with an agent running at the board |

`pio remote device monitor` needs a terminal; from a script use
`timeout 30s script -qfc "pio remote device monitor -b 115200" log.txt`.
Never run it unbounded. Opening the monitor resets the board.

### 4.2 Builds and tests

| Environment | Use |
|---|---|
| `esp32s2` | Live board (LOLIN S2 mini, v0.1) |
| `esp32s2_counter_preset` | One-shot relay counter preset |
| `esp32s2_password_reset`, `esp32dev_sim_password_reset` | One-shot factory passwords (flash over USB) |
| `esp32dev_sim_ota` | Bench: emulated v0.1 board, uploads over HTTP |
| `esp32c3_hhn1x`, `esp32s3_hhn1x`, `esp32dev_hhn1x_sim` | v1.x board |
| `esp32dev`, `esp32dev_sim` | Boardless / emulated, USB (`esp32dev_sim` is the default) |

```bash
cd firmware
pio run -e esp32s2                                    # build; all environments build without warnings
pio run -e esp32s2 -t buildfs                         # portal files image
# upload to the S2 only after the owner's "go":
HPN_PORTAL_PASSWORD=admin python3 tools/http_ota.py .pio/build/esp32s2/firmware.bin localhost:30081
HPN_PORTAL_PASSWORD=admin python3 tools/http_ota.py .pio/build/esp32s2/littlefs.bin localhost:30081

cd ../floor_simulator && .venv/bin/python -m pytest -q     # incl. firmware MPC + L2 on the host
```

Firmware changes to L2 or the MPC core are checked on the host:
`firmware/test/host/l2_harness.cpp` (via `floor_simulator/tests/test_fw_l2.py`)
and the simulator's closed loop with the firmware MPC (`test_fw_mpc.py`,
`floor_simulator/tools/mixer_tuning_sim.py`, preset `cold_slab_start` for heating
starts). Factory credentials are set at build time in the git-ignored
`include/hpn/credentials_local.h`. After editing `firmware/data/`, regenerate
the `.gz` copies (`gzip -9 -n -k -f <file>`): the filesystem image carries
only those. Observations of the plant are best checked against the observer's
InfluxDB (it recorded the heating start that revealed the MPC warm-up stall).

## 5. Architecture in ten minutes

### 5.1 Layers and pin ownership

```
HA / portal / MQTT
        │  *_intended
        ▼
  L4  comms    (T_comms, 50 ms)      ── Wi-Fi, MQTT, HTTP/WS, HPN1 auth, LEDs
        │  intents, API
        ▼
  L2  control  (T_control, 2000 ms)  ── coordination, MPC or PI, mixer target
        │  *_requested, mix_target_pos, dhw_active
        ▼
  L1  safety   (T_safety, 500 ms)    ── limits, arbitration, mixer servo
        │  *_phys, mix_motion/mix_dir, alarms
        ▼
  L0  I/O      (T_io, 20 ms)         ── the ONLY layer that touches pins
        │  relays, expander, MUX, LEDs, 1-Wire, current sensing
        ▼
   board (74HC595 / PCF8574) or emulation model
```

- **Only L0 touches hardware**, and the output driver has one writer (`T_io`).
  On v0.1 the 74HC595 carries relay bits and MUX select lines; on v1.x the
  PCF8574 carries the relay bits and the P7 fault input.
- **L1 always wins.** L2 and the portal may clamp, but L1 re-checks every move
  and never opens past the effective ceiling.
- **`T_io` is the only watchdog feeder**, and only while `T_safety`, `T_io` and
  `T_control` meet their deadlines. A withheld feed is logged (events 1900/1901)
  and kept in RTC memory (`lastWatchdog` after the reset). Timeout
  `max(tick_ms*4 + 1000, 5000)` = 9 s.

### 5.2 Tasks

| Task | Period | Priority | Core (dual-core) |
| --- | --- | --- | --- |
| `T_safety` | 500 ms | highest | 1 |
| `T_io` | 20 ms | high | 1 |
| `T_control` | `tick_ms` (2000) | medium; **below lwIP and the web server on single-core chips** | 1 |
| `T_comms` | 50 ms | low | 0 |
| `T_logger` | 1000 ms | lowest | 0 |

On single-core parts (S2, C3) all tasks are unpinned and share the core with
Wi-Fi; heavy work there starves the network.

### 5.3 The relay states

Every pump carries four booleans; confusing them is the most common mistake:

- `*_intended`: last accepted HA/Test Mode command (L4);
- `*_demand`: effective demand after the keep-alive (L2); a missed keep-alive
  drops it while `*_intended` stays set;
- `*_requested`: what coordination and after-run decided (L2);
- `*_phys`: what L1 arbitrated and L0 drives.

The heater has no HA command: L2 derives it from the three circuit requests.

### 5.4 Configuration

- `struct Config` (1376 bytes, `include/hpn/config.h`) is stored as one NVS
  blob: header (magic, version, size), payload, CRC32. Format version 5.
- **No migration.** A blob of another version is ignored and the compiled
  defaults apply, Wi-Fi included. Board facts that are not user settings go in
  `BoardConfig` (`src/board.cpp`), not in `Config`.
- Other NVS records (relay counters `hpnctr`, anti-seize `hpnanti`, security
  `hpnsec`, MPC parameters `hpnmpc`) follow the same rule.
- Backup and restore: `GET /api/config/export` (sealed), `POST /api/config/import`.

### 5.5 Mixer

- L2 writes only `mix_target_pos`; L1 is the servo against the dead-reckoned
  position and never energises the motor past the ceiling.
- **MPC** (default) plans moves over a 15-minute horizon with per-scenario
  parameter sets updatable by the observer; on anomalies it falls back to the
  cautious legacy PI and homes the valve. See `predictive-mixer.md`.
- **Ceiling** = `min(mix_max_open_pct, dynamic max)`; the **minimum**
  (`mix_min_open_pct`) applies only to normal floor control, never to a close.
- Mixer anomalies are warnings; they do not stop the motor.

### 5.6 Safety hierarchy (L1), in order

1. Hard over-temperature: latches 101, floor off, mixer closed.
2. High-temperature warning band: no further opening.
3. Gradient cutoff.
4. Sensor postures: mixer sensor loss critical (201), return loss warning
   (202), boiler loss blocks heating (203).
5. Role plausibility (only while heating): `boiler ≥ forward ≥ return`.
6. Heater lockout: summer mode, anti-seize.
7. Anti-short-cycle backstop; demand loss (301) and after-run belong to L2.
8. Pump plausibility: dry-run and welded relay from measured power, gated by
   `pump_power_check_enabled`.

DHW: `dhw_active = meter || inferred`; the meter wins.

### 5.7 Communications (L4)

- Portal on port 80 (AsyncWebServer). Every API call is HPN1-signed; `monitor`
  is read-only. WebSocket `/ws` pushes state at 4 Hz (1 Hz on single-core
  chips), built in PSRAM when present.
- **Pre-size large JSON responses** (`send_json()` uses `measureJson()`).
- MQTT/HA discovery; MPC commands over MQTT are signed with the shared secret.
- Logs are RAM-only (96 events, 180 slow samples, 64 access records); relay
  counters (operations) persist hourly in hardware builds.
- OTA over HTTP for app and filesystem, admin-signed, with rollback.
- Built-in maintenance page at `/maintenance` (works without portal files).

## 6. Gotchas and invariants

1. **Never feed the watchdog from network code.** Keep
   `-DCONFIG_ASYNC_TCP_USE_WDT=0` in every environment.
2. **Never run the serial monitor unbounded.**
3. **`*_intended` ≠ `*_demand` ≠ `*_requested` ≠ `*_phys`.** Debug the right stage.
4. **A minimum opening never blocks a close.**
5. **L1 is authoritative.** Never clamp only in the UI.
6. **Sensor-role verification needs active heating**; "not measurable" on an
   idle plant is correct.
7. **The bench is dual-core, has an FPU and emulates the I/O.** Single-core,
   FPU-less and real-I/O problems show only on hardware; a green
   `esp32dev_sim` run proves logic, not wiring.
8. **Mind the S2's internal RAM.** Large or long-lived buffers go to PSRAM;
   check the heap numbers after changes that allocate.
9. **Flashing replaces the running plant firmware.** Do it with pumps and
   boiler disabled or with explicit intent; OTA carries the same risk.
10. **Live checks change settings** (passwords, secret) and restore them; they
    expect factory passwords.
11. **No credentials in the repository**, including the ESPHome OTA password.
12. `floor_simulator/` and `observer/`: each uses its own `.venv` (uv) and has
    its own tests.

## 7. Where to read more

| Question | Document |
| --- | --- |
| Targets, tasks, portal, NVS, LEDs | `README.md` |
| Board wiring | `docs/boards.md` |
| Emulation model | `docs/io-emulation.md` |
| Predictive mixer control | `docs/predictive-mixer.md` |
| Security design and reasons | `docs/security.md` |
| Partition layout, migration, rollback, fallback AP | `docs/partition-migration.md` |
| ESPHome upgrade checklist | `docs/esphome-upgrade.md` |
| Simulator | `../floor_simulator/README.md`, `../floor_simulator/specification.md` |
| Observer | `../observer/HANDOVER.md` |
| System architecture | `../plans/master_plan.md` |
| Layer model, contracts, L0–L2, modes, interfaces, portal | `../plans/controller node/` |
| Old-system data and analysis | `../analysis/handover.md` |
| Change history | `git log` |
