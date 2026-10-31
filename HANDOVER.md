# HeatpumpsN — project handover

The single entry point for picking the project up: what the system is, where it
stands, how to work on it and what to keep in mind. Updated 2026-10-31. The
data-analysis workstream has its own handover in
[`analysis/handover.md`](analysis/handover.md).

## 1. What the system is

A residential heating distribution controller with three hydraulic circuits and
one shared boiler/heater call:

| Circuit | Actuators | Temperature |
| --- | --- | --- |
| Radiator 1 (unmixed) | `pump_rad1` | boiler supply |
| Radiator 2 (unmixed) | `pump_rad2` | boiler supply |
| Floor (mixed) | `pump_floor` + 3-point motorised mixer | blended floor supply |

The ESP32 owns the real-time and safety-critical part: it arbitrates heater and
pump requests, derives the shared heater call from the three circuit demands,
drives the mixer, owns the per-circuit pump after-run and supervises limits.
Home Assistant owns comfort logic and sends three per-circuit heating-switch
demands, repeating each active `ON` as a keep-alive; there is no heater switch.
The mixer has **no absolute position sensor**: position is dead-reckoned from
travel time.

## 2. Where things stand

| Area | State | Details |
|---|---|---|
| Controller firmware 0.4 (`firmware/`) | Working on the bench (ESP32, emulated board) and on the live LOLIN S2 mini (HomeNet v0.1) | `firmware/README.md` |
| Predictive mixer control (MPC) | Implemented with supervised fallback to the legacy PI and homing; tuned against the simulator | `firmware/docs/predictive-mixer.md` |
| Security (HPN1) | Challenge-response request signing, users `admin`/`monitor`, signed uploads, sealed secrets, signed MQTT commands | `firmware/docs/security.md` |
| Partition layout / ESPHome migration | Done on the bench and on the live S2 | `firmware/docs/partition-migration.md` |
| HIL floor simulator (`floor_simulator/`) | Plant, boiler, REST/MQTT bridges, dashboard, offline tuning tools | `floor_simulator/README.md` |
| HomeNet v1.x board | Firmware profile exists; hardware not built | `firmware/docs/boards.md` |

### 2.1 Live S2 upgrade (in progress)

Checklist: `firmware/docs/esphome-upgrade.md`.

- [x] 1–4: ESPHome OTA → maintenance page → partition migration → portal files
- [x] Relays checked in Test Mode
- [x] Status LEDs: driver fixed, colour test, board order (pump 2 on LED 3, pump 1 on LED 4), radiators green
- [ ] 5: change the factory passwords (still admin/admin)
- [ ] 6.2: temperature sensors: discover, assign roles, verify
- [ ] 6.5: settings (floor circuit, setpoint, summer mode, MQTT)
- [ ] 7: relay counter preset (`esp32s2_counter_preset`, ESPHome values 1:1)
- [ ] 8: normal firmware, configuration export, mains on, first heating run watched

Facts about this board:

- It keeps **ESPHome's bootloader**, which supports rollback. A new image is
  confirmed after 2 min with Wi-Fi, or at once before an admin-requested
  reboot; otherwise the next reboot rolls it back.
- Wi-Fi `Korona-IoT`, IP 192.168.2.74. The fallback AP `HeatpumpsN-Setup` opens
  after 2 min without a connection (captive portal at 192.168.4.1).
- The S2 has no FPU and one core; internal RAM is the limit. PSRAM (2 MB) takes
  ordinary allocations from 512 B, the logs and the state frames. Watch
  `freeHeap`, `minFreeHeap` and `largestFreeBlock` in `/api/status`
  (last seen: ~40 KB free, low-water mark ~28–34 KB).

## 3. Open issues

1. **MPC compute time on the S2** is unmeasured: no plan has run there yet. The
   control task runs below lwIP on single-core chips, but a step longer than
   3 periods (6 s) trips the watchdog. Check `computeMs` in `/api/mpc` during
   the first heating run; `lastWatchdog` in `/api/status` names the late task
   after a watchdog reset.
2. **Network latency on the S2**: pings of 0.2–5 s before the memory fixes; not
   re-measured since.
3. **One unexplained interrupt-watchdog reset** on the bench, on the first boot
   of one image; the same image ran cleanly afterwards. No coredump.
4. **Home Assistant keep-alive**: the three heating switches must repeat their
   `ON` faster than `demand_timeout_s` (600 s); a missed refresh raises `301`
   and drops the demand by design. Configure this when the S2 joins MQTT.
5. **Per-circuit pump after-run**: the firmware keeps separate timers, the
   configuration still exposes one `pump_stop_after_heating_s` for all three.
   Decide whether per-circuit values are needed.
6. **LED 0 blue for the AP / captive portal** (plan §1.2) is not implemented,
   although the fallback AP exists.
7. **v1.x hardware**: schematic open items (ESP32-S3 pin map, TLV9064 bias,
   mains protection), then PCF8574, ADS1115 and the interlock fault line on real
   hardware, and the current calibration against a clamp meter.
8. **Mixer encoder** (`POS_SRC_I2C`) and other reserved I2C peripherals: not
   implemented; an external encoder over MQTT is used for warnings and resync
   only.

## 4. Repository map

| Path | What it is |
| --- | --- |
| `HANDOVER.md` | this file |
| `README.md` | short project overview |
| `AGENTS.md` | rules for agents: remote agent, bounded serial monitor |
| `firmware/` | PlatformIO project: controller firmware, portal assets (`data/`), tools (`tools/`) |
| `firmware/docs/` | boards, I/O emulation, predictive mixer, security, partition migration, ESPHome upgrade checklist |
| `firmware/test/live/` | live checks against a running controller (HPN1 security) |
| `floor_simulator/` | HIL simulator: plant, boiler, bridges, dashboard, tuning tools, tests |
| `tests/` | older runtime integration checks against a live controller |
| `plans/` | system architecture, controller-node layer specs, observer and MQTT designs, schematics |
| `analysis/` | old-system telemetry analysis (DuckDB), reports, its own handover |

Not tracked on purpose: `analysis/heatpumps.duckdb`, `analysis/db/`, `others/`,
`.pio/`, `firmware/include/hpn/credentials_local.h`, secrets.

## 5. Working on it

### 5.1 Access from the development machine

| What | Where |
|---|---|
| Bench controller (ESP32, `esp32dev_sim_ota`) | `http://localhost:30080` |
| Live S2 (when forwarded) | `http://localhost:30081` |
| MQTT broker | `localhost:31883` |
| Serial, flashing over USB | `pio remote …` with an agent running at the board |

`pio remote device monitor` needs a terminal; from a script use
`timeout 30s script -qfc "pio remote device monitor -b 115200" log.txt`.
Never run it unbounded. Opening the monitor resets the board.

### 5.2 Builds (`firmware/`)

| Environment | Use |
|---|---|
| `esp32dev_sim_ota` | Bench: emulated v0.1 board, uploads over HTTP |
| `esp32s2` | Live board (LOLIN S2 mini, v0.1) |
| `esp32s2_counter_preset` | One-shot relay counter preset |
| `esp32s2_password_reset`, `esp32dev_sim_password_reset` | One-shot factory passwords (flash over USB) |
| `esp32c3_hhn1x`, `esp32s3_hhn1x`, `esp32dev_hhn1x_sim` | v1.x board |
| `esp32dev`, `esp32dev_sim` | Boardless / emulated, USB (`esp32dev_sim` is the default) |

```bash
pio run -e esp32dev_sim_ota -t upload          # HPN_PORTAL_PASSWORD=<admin password>
pio run -e esp32dev_sim_ota -t uploadfs
python3 tools/http_ota.py .pio/build/esp32s2/firmware.bin localhost:30081   # any build, any host
python3 tools/partition_check.py               # before a migration
python3 test/live/security_check.py            # needs factory passwords
```

All environments build without warnings; keep it that way (`-Wall -Wextra` on
project sources). Factory credentials are set at build time in the git-ignored
`include/hpn/credentials_local.h` (see the `.example`). After editing
`data/`, regenerate the `.gz` copies (`gzip -9 -n -k -f <file>`): the
filesystem image carries only those.

## 6. Architecture in ten minutes

### 6.1 Layers and pin ownership

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

### 6.2 Tasks

| Task | Period | Priority | Core (dual-core) |
| --- | --- | --- | --- |
| `T_safety` | 500 ms | highest | 1 |
| `T_io` | 20 ms | high | 1 |
| `T_control` | `tick_ms` (2000) | medium; **below lwIP and the web server on single-core chips** | 1 |
| `T_comms` | 50 ms | low | 0 |
| `T_logger` | 1000 ms | lowest | 0 |

On single-core parts (S2, C3) all tasks are unpinned and share the core with
Wi-Fi; heavy work there starves the network.

### 6.3 The relay states

Every pump carries four booleans; confusing them is the most common mistake:

- `*_intended`: last accepted HA/Test Mode command (L4);
- `*_demand`: effective demand after the keep-alive (L2); a missed keep-alive
  drops it while `*_intended` stays set;
- `*_requested`: what coordination and after-run decided (L2);
- `*_phys`: what L1 arbitrated and L0 drives.

The heater has no HA command: L2 derives it from the three circuit requests.

### 6.4 Configuration

- `struct Config` (1376 bytes, `include/hpn/config.h`) is stored as one NVS
  blob: header (magic, version, size), payload, CRC32. Format version 5.
- **No migration.** A blob of another version is ignored and the compiled
  defaults apply, Wi-Fi included. Board facts that are not user settings go in
  `BoardConfig` (`src/board.cpp`), not in `Config`.
- Other NVS records (relay counters `hpnctr`, anti-seize `hpnanti`, security
  `hpnsec`, MPC parameters `hpnmpc`) follow the same rule.
- Backup and restore: `GET /api/config/export` (sealed), `POST /api/config/import`.

### 6.5 Mixer

- L2 writes only `mix_target_pos`; L1 is the servo against the dead-reckoned
  position and never energises the motor past the ceiling.
- **MPC** (default) plans moves over a 15-minute horizon with per-scenario
  parameter sets updatable by the observer; on anomalies it falls back to the
  cautious legacy PI and homes the valve. See `predictive-mixer.md`.
- **Ceiling** = `min(mix_max_open_pct, dynamic max)`; the **minimum**
  (`mix_min_open_pct`) applies only to normal floor control, never to a close.
- Mixer anomalies are warnings; they do not stop the motor.

### 6.6 Safety hierarchy (L1), in order

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

### 6.7 Communications (L4)

- Portal on port 80 (AsyncWebServer). Every API call is HPN1-signed; `monitor`
  is read-only. WebSocket `/ws` pushes state at 4 Hz (1 Hz on single-core
  chips), built in PSRAM when present.
- **Pre-size large JSON responses** (`send_json()` uses `measureJson()`).
- MQTT/HA discovery; MPC commands over MQTT are signed with the shared secret.
- Logs are RAM-only (96 events, 180 slow samples, 64 access records); relay
  counters (operations) persist hourly in hardware builds.
- OTA over HTTP for app and filesystem, admin-signed, with rollback.
- Built-in maintenance page at `/maintenance` (works without portal files).

## 7. Gotchas and invariants

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
12. `floor_simulator/`: use its `.venv` (uv); tests in `floor_simulator/tests`.

## 8. Where to read more

| Question | Document |
| --- | --- |
| Targets, tasks, portal, NVS, LEDs | `firmware/README.md` |
| Board wiring | `firmware/docs/boards.md` |
| Emulation model | `firmware/docs/io-emulation.md` |
| Predictive mixer control | `firmware/docs/predictive-mixer.md` |
| Security design and reasons | `firmware/docs/security.md` |
| Partition layout, migration, rollback, fallback AP | `firmware/docs/partition-migration.md` |
| ESPHome upgrade checklist | `firmware/docs/esphome-upgrade.md` |
| Simulator | `floor_simulator/README.md`, `floor_simulator/specification.md` |
| System architecture | `plans/master_plan.md` |
| Layer model, contracts, L0–L2, modes, interfaces, portal | `plans/controller node/` |
| Old-system data and analysis | `analysis/handover.md` |
| Change history | `git log` |
