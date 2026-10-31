# Handover

State of the project and how to pick it up. Updated 2026-10-31.

## 1. Where things stand

| Area | State | Details |
|---|---|---|
| Controller firmware 0.4 (`firmware/`) | Working on the bench (ESP32, emulated board) and on the live LOLIN S2 mini (HomeNet v0.1) | `firmware/README.md` |
| Predictive mixer control (MPC) | Implemented with supervised fallback and homing; tuned against the simulator | `firmware/docs/predictive-mixer.md` |
| Security (HPN1) | Challenge-response request signing, users `admin`/`monitor`, signed uploads, sealed secrets, signed MQTT commands | `firmware/docs/security.md` |
| Partition layout / ESPHome migration | Done on the bench and on the live S2 | `firmware/docs/partition-migration.md` |
| HIL floor simulator (`floor_simulator/`) | Plant, boiler, REST/MQTT bridges, dashboard, offline tuning tools | `floor_simulator/README.md` |
| HomeNet v1.x board | Firmware profile exists; hardware not built | `firmware/docs/boards.md` |

## 2. Live S2 upgrade (in progress)

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
  after 2 min without a connection.
- The S2 has no FPU and one core; internal RAM is the limit. PSRAM (2 MB) now
  takes ordinary allocations from 512 B, the logs and the state frames. Watch
  `freeHeap`, `minFreeHeap` and `largestFreeBlock` in `/api/status`
  (last seen: ~40 KB free, low-water mark ~28–34 KB).

## 3. Open issues

1. **MPC compute time on the S2** is unmeasured: no plan has run there yet. The
   control task runs below lwIP on single-core chips, but a step longer than
   3 periods (6 s) trips the watchdog. Check `computeMs` in `/api/mpc` during
   the first heating run; `lastWatchdog` in `/api/status` names the late task
   after a watchdog reset.
2. **Network latency on the S2**: pings of 0.2–5 s before the memory fixes. Not
   re-measured since.
3. **One unexplained interrupt-watchdog reset** on the bench, on the first boot
   of one image; the same image ran cleanly afterwards. No coredump.
4. **LED 0 blue for the AP / captive portal** (plan §1.2) is not implemented,
   although the fallback AP now exists.
5. **v1.x hardware**: schematic open items (ESP32-S3 pin map, TLV9064 bias,
   mains protection), then PCF8574, ADS1115 and the interlock fault line on real
   hardware, and the current calibration against a clamp meter.
6. **Mixer encoder** and other reserved I2C peripherals: not implemented.

## 4. Working on it

### Access from the development machine

| What | Where |
|---|---|
| Bench controller (ESP32, `esp32dev_sim_ota`) | `http://localhost:30080` |
| Live S2 (when forwarded) | `http://localhost:30081` |
| MQTT broker | `localhost:31883` |
| Serial, flashing over USB | `pio remote …` with an agent running at the board |

`pio remote device monitor` needs a terminal; from a script use
`timeout 30s script -qfc "pio remote device monitor -b 115200" log.txt`.
Never run it unbounded. Opening the monitor resets the board.

### Builds (`firmware/`)

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
`include/hpn/credentials_local.h` (see the `.example`).

### Rules that cost time when forgotten

- **No NVS migration.** Saved settings in another format are dropped and the
  build defaults apply (Wi-Fi included). Avoid changing `Config` casually; board
  facts belong in `BoardConfig`.
- **The bench is dual-core, has an FPU and emulates the I/O.** Single-core,
  FPU-less and real-I/O problems (S2) only show on hardware.
- **No credentials in the repository**, including the ESPHome OTA password.
- `floor_simulator/`: use its `.venv` (uv), tests under `floor_simulator/tests`
  (`.venv/bin/python -m pytest`).
