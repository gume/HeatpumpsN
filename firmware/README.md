# HeatpumpsN firmware

> **Session handover:** [`HANDOVER.md`](HANDOVER.md) tracks in-progress work,
> open issues and the exact device/build state. Read it before resuming.

PlatformIO project for the ESP32 controller node. It currently implements a
FreeRTOS task set, the boot-safe **L0** output layer, a carrier abstraction for
HomeNet Heatpumps 1.0 and 2.0, a compile-time **I/O emulation** backend so the
controller can be developed on a boardless devkit, a station-mode **Wi-Fi
client**, a **MQTT/Home Assistant** integration, and a LittleFS-backed **web
portal** in L4. Production L1 safety arbitration, L2 control and L3 adaptation
are still TODO; the bench-only loopback used by Test Mode is described below.
The next implementation steps are described in
[`../plans/controller node/`](../plans/controller%20node/).

## Targets, carriers and backends

| Environment | MCU board | Core | Carrier | Backend |
| --- | --- | --- | --- | --- |
| `esp32dev_sim` | `esp32dev` | dual-core Xtensa | HomeNet Heatpumps 1.0 | **emulated** — default env |
| `esp32dev` | `esp32dev` | dual-core Xtensa | boardless | hardware (no pins assigned) |
| `esp32s2` | `lolin_s2_mini` | single-core Xtensa | HomeNet Heatpumps 1.0 | hardware, 74HC595 + MUX |
| `esp32s3` | `esp32-s3-devkitc-1` | dual-core Xtensa | boardless | hardware, 2.0 target |
| `esp32c3` | `esp32-c3-devkitm-1` | single-core RISC-V | boardless | hardware, 2.0 target |

Carriers describe wiring; the backend describes whether writes reach pins. They
are independent compile-time choices:

- `-DHPN_CARRIER_HHN1` — HomeNet Heatpumps 1.0: 74HC595 outputs, analogue
  current MUX, 5 LEDs, 1-Wire. ESP32-S2 in the field.
- `-DHPN_CARRIER_BOARDLESS` — bare devkit, pins unassigned
  (`HPN_GPIO_UNASSIGNED`) until the 2.0 layout exists.
- `-DHPN_IO_EMULATION` — L0 runs against an in-memory model of the carrier
  instead of pins.

The HomeNet Heatpumps 1.0 carrier is documented in
[`docs/hhn-1.0.md`](docs/hhn-1.0.md): pin map, 74HC595 bit map, MUX channels,
ACS712 scaling, 1-Wire ROM IDs, LED mapping and the safety caveats of that
wiring. The 2.0 carrier is still under planning and has no profile yet;
ESP32-C3 and ESP32-S3 are its primary targets.

The emulation model, its API and the bench web page are described in
[`docs/io-emulation.md`](docs/io-emulation.md).

`esp32s2` uses the `lolin_s2_mini` board definition; switch it to
`lolin_s2_pico` in `platformio.ini` if the fitted MCU is a PICO.

## RTOS task set

`hpn::tasks::start()` creates five permanent tasks and then deletes the Arduino
`loopTask`, so only the planned tasks remain.

| Task | Period | Priority | Dual-core affinity | Purpose |
| --- | --- | --- | --- | --- |
| `T_safety` | 500 ms | highest | Core 1 | L1 safety arbitration |
| `T_io` | 20 ms | high | Core 1 | L0 hardware access, watchdog feeder |
| `T_control` | `config.tick_ms` (default 2000 ms) | medium | Core 1 | L2 control and coordination |
| `T_comms` | 50 ms | low | Core 0 | L4 MQTT / HTTP / commands |
| `T_logger` | 1000 ms | lowest | Core 0 | Logger task (portal owns event/slow logs) |

Notes:

- On single-core parts (ESP32-S2, ESP32-C3, `HPN_HAS_DUAL_CORE == 0`) all tasks
  are created with `xTaskCreate`; none is pinned to a non-existent Core 1.
- Health supervision covers `T_safety`, `T_io` and `T_control` only. `T_comms`
  and `T_logger` are deliberately excluded so a network stall cannot trip the
  watchdog.
- `T_io` subscribes to the task watchdog and feeds it only while the real-time
  tasks are within their deadlines. A hung safety or control task therefore
  resets the controller instead of leaving outputs latched.
- Task bodies call the corresponding layer `tick()`. L0, the Wi-Fi client,
  MQTT/HA and the portal are implemented; L1/L2 currently contain only the
  bench loopback used by Test Mode.

## Wi-Fi client

`hpn::wifi` ([`include/hpn/wifi_client.h`](include/hpn/wifi_client.h),
[`src/wifi_client.cpp`](src/wifi_client.cpp)) is a minimal station-mode client
started by L4:

- credentials come from `Config::wifi_ssid` / `Config::wifi_password`; both are
  editable from the portal's **Network & MQTT** page and persisted to NVS
- association is non-blocking; `T_comms` supervises the link and retries every
  10 s
- modem sleep is off, so the link stays responsive for the portal
- if the configured AP is not visible, the client resets the station and logs
  one diagnostic scan of nearby 2.4 GHz SSIDs
- link state, RSSI and the DHCP address are logged

Current bench defaults in `config_defaults()` are SSID `Testing` and password
`testing123`. The SSID is case-sensitive. The Network page can already update
and persist credentials in NVS; these compiled-in values are only the
first-boot fallback until the captive-portal provisioning flow exists.

Verified on the remote ESP32 (`esp32dev_sim`): association with `Testing`
succeeds, RSSI ≈ −74 dBm, DHCP assigns `192.168.2.14`, and the portal is
reachable through the user's tunnel at `http://localhost:9090/`.

## MQTT and Home Assistant

`hpn::mqtt` ([`include/hpn/mqtt_client.h`](include/hpn/mqtt_client.h),
[`src/mqtt_client.cpp`](src/mqtt_client.cpp)) uses
[`knolleary/PubSubClient`](https://github.com/knolleary/pubsubclient) and the
official [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
schema. PubSubClient was already the project dependency and is a good fit for a
small, single-broker controller; the discovery payloads are written explicitly
instead of adding an abstraction layer. MQTT is enabled when
`Config::mqtt_host` is non-empty. The broker host/IP, port, optional username
and password, and base topic are edited on **Settings → Network & MQTT**.
The broker must be reachable from the controller's Wi-Fi network; an empty host
disables MQTT and clears the connection state.

The controller uses `<base>/status` for availability (`online` / `offline`,
retained, with a last-will message), `<base>/cmd/...` for commands, and
`<base>/state/...` for entity state. Discovery is published retained under
`homeassistant/<component>/<node_id>/<object_id>/config`, where `<node_id>` is
derived from the Wi-Fi MAC address.

### Entities

| HA domain | Entity | State topic | Command topic |
| --- | --- | --- | --- |
| `switch` | Heater | `state/heater` | `cmd/heater` |
| `switch` | Pump floor | `state/pump_floor` | `cmd/pump_floor` |
| `switch` | Pump radiator 1 | `state/pump_rad1` | `cmd/pump_rad1` |
| `switch` | Pump radiator 2 | `state/pump_rad2` | `cmd/pump_rad2` |
| `binary_sensor` | Heater virtual | `state/heater_virtual` | — |
| `binary_sensor` | Heater physical | `state/heater_physical` | — |
| `binary_sensor` | Pump floor virtual | `state/pump_floor_virtual` | — |
| `binary_sensor` | Pump floor physical | `state/pump_floor_physical` | — |
| `binary_sensor` | Pump radiator 1 physical | `state/pump_rad1_physical` | — |
| `binary_sensor` | Pump radiator 2 physical | `state/pump_rad2_physical` | — |
| `sensor` | Floor pump power | `state/power_floor` | — |
| `sensor` | Radiator 1 pump power | `state/power_rad1` | — |
| `sensor` | Radiator 2 pump power | `state/power_rad2` | — |
| `sensor` | Mixer motor power | `state/power_mixer` | — |
| `sensor` | Mixer state (`idle`/`opening`/`closing`) | `state/mixer_state` | — |
| `sensor` | Boiler supply temperature | `state/temp_boiler` | — |
| `sensor` | Floor return temperature | `state/temp_return` | — |
| `sensor` | Mixer forward temperature | `state/temp_mix` | — |
| `number` | Floor setpoint | `state/floor_setpoint` | `cmd/floor_setpoint` |

The explicit request named “5 binary sensors” but listed heater virtual,
heater physical, pump 1 physical, pump 2 physical, and pump floor virtual and
physical. The implementation exposes **six** because the floor virtual and
floor physical states are separate entities. Remove one discovery call in
`publish_discovery()` if five was intended.

Switch state topics carry the physical relay state. The virtual binary sensors
carry the accepted `*_intended` command, while the physical binary sensors
carry the L1/L0 output state. The floor setpoint command updates
`Config::floor_setpoint_shadow`; it is an HA-owned runtime shadow, not a
Category 1 configuration value. MQTT commands set `*_intended`, so on the bench
they flow through `HPN_BENCH_LOOPBACK` to the emulated carrier; the production
L2/L1 path is still TODO.

State and discovery are retained. The client reconnects every 5 s while the
broker is unreachable, and the portal reports `mqtt.state`, broker, connect
counts and error count through `/api/status` and the live WebSocket snapshot.
`POST /api/mqtt/test` queues a non-retained test publication; it returns `202`
when the broker is not connected yet and `409` when no broker host is set.

## Relay counter persistence

Relay cycles and runtime live in the portal's RAM cache. In hardware builds
(`!HPN_IO_EMULATION`) they are also stored in the separate `hpnctr` NVS
namespace:

- counters are loaded at boot;
- a dirty flag is set by a cycle change or by the one-second runtime
  accumulation;
- the cache is written at most once per hour, and only when dirty;
- an explicit counter reset requests an immediate save so a reboot cannot
  resurrect the old values;
- emulation builds never load or save counters, so the bench model cannot wear
  the NVS or leak test data into a hardware configuration.

## Web portal

The portal is a vanilla HTML/CSS/JS single-page app served from LittleFS. L4
starts it after Wi-Fi connects; the HTTP server runs on port 80 in the AsyncTCP
task and never blocks the real-time tasks.

- `GET /` serves `data/index.html`; assets live under `/css/` and `/js/`.
- `GET /ws` is the WebSocket carrying the live state snapshot at 4 Hz
  (250 ms).
- REST endpoints under `/api/` cover state, status, runtime, configuration
  sections, Test Mode, logs, relay counters, system actions and OTA.
- Precompressed `.gz` siblings are served when the client sends
  `Accept-Encoding: gzip`. Regenerate them after editing an asset:

  ```bash
  gzip -9 -c data/js/app.js > data/js/app.js.gz
  ```

### Pages

- **Dashboard** — mode, uptime, heap, Wi-Fi, Test Mode, circuits, temperatures,
  heater/safety state, the five carrier status LEDs and recent events.
- **Plant** — read-only circuits, pumps, after-run timers and mixer feedback.
- **Diagnostics** — Event Log, Slow Log (10 s samples), Sensor Health and Relay
  Counters.
- **Test Mode** — time-limited manual control through the same
  `*_intended` → `*_requested` intake as HA. The heater is refused unless a pump
  is commanded ON. Activation/deactivation and every command are logged.
- **Settings** — Safety & Timeouts, Hardware & Pins, Network & MQTT, Firmware
  Update and System. Saves are validated server-side and written to NVS only
  when the form is submitted. The Network page shows the live MQTT state and
  can queue a test publication.
- **Runtime Parameters** — read-only HA/observer values.

The event, access and slow logs are RAM-only in this build; they reset on
reboot. Relay counters are persisted hourly in hardware builds and stay in RAM
under emulation (see above). Configuration is persisted in a versioned NVS
blob. The access log records every portal request plus WebSocket
connect/disconnect events.

### Test Mode and the bench loopback

`HPN_BENCH_LOOPBACK` is compiled only into `esp32dev_sim`. L2 copies
`*_intended` to `*_requested`, and L1 applies those requests to the emulated
carrier. The only L1 interlock in this path is "heater requires an active
pump"; over-temperature, gradient, sensor-posture and demand/after-heating
rules remain TODO(L1). The loopback is a bench aid, **not** safety
arbitration, and never appears in a hardware build.

Test Mode expires after `safety.test_mode_max_s` (default 7200 s).
Deactivation clears manual outputs and returns the controller to its configured
mode. The anti-seize sequence is implemented; mixer-sweep, endstop-home and
heater-pump return HTTP 501 until their observers exist.

### Emulation page

When `HPN_IO_EMULATION` is defined, `GET /emulation.html` serves a separate
bench page for the in-memory carrier: compact relay buttons, mixer
open/stop/close, follow-controller and realistic-behaviour switches,
temperature/power inputs (0–40 W), a carrier readout and a reset button. It is
a direct board control surface and deliberately bypasses L1; there is no
heater/pump interlock on that page. The page is not linked from the main menu
and does not exist in hardware builds. Status LEDs live on the main dashboard.
See [`docs/io-emulation.md`](docs/io-emulation.md).

## Commands (remote agent)

The target board is attached to a remote runner. The upload commands below
include `-d firmware` so they work from the repository root; drop that option
when you are already in `firmware/`.

```bash
pio remote run -e esp32dev_sim                # daily dev: boardless + emulated HHN 1.0
pio remote run -e esp32s2                     # real HHN 1.0 carrier
pio remote run --force-remote -d firmware --target uploadfs   # LittleFS assets
pio remote run --force-remote -d firmware --target upload     # application
timeout 10s pio remote device monitor -b 115200 || true
```

`--force-remote` makes PlatformIO Remote re-sync the project instead of reusing
its cached copy; use it after editing frontend assets or firmware sources.
Upload the filesystem **before** the application when both changed, then reset
the board (the uploader does this automatically) so LittleFS and the app match.

Always bound `pio remote device monitor` with `timeout`; an unbounded monitor
blocks the session. The monitor also needs a terminal: `pio remote device
monitor` refuses piped or redirected stdin. For scripted captures, wrap it in
`script`, for example:

```bash
timeout 15s script -qec "pio remote device monitor -b 115200" /dev/null 2>&1 \
  | sed -r 's/\x1B\[[0-9;]*[mK]//g' | tr -d '\r'
```

The remote agent is authenticated through the PlatformIO account
(`pio account login`). Check availability with `pio remote agent list`, and
start it on the runner with `pio remote agent start`.

`pio remote run` without `-e` builds `esp32dev_sim`, the default environment:
boardless devkit with the emulated Heatpumps 1.0 carrier. Name a hardware
environment explicitly when the target must drive real pins.

Local `pio run` remains available as an offline compile-only fallback, but
build/flash/monitor operations for the target board use the remote agent.

### Remote upload workaround

PlatformIO Remote uploads both the application and the filesystem with the
`nobuild` target. That skips the ESP32 platform's `checkprogsize`/partition
parsing and the Arduino framework script, so `PARTITIONS_TABLE_CSV` and
`ESP32_APP_OFFSET` are empty. `uploadfs` then aborts with `Could not find the
file  with partitions table`, and `upload` calls esptool with `Address
".pio/build/<env>/firmware.bin" must be a number`.

`src/remote_upload.json` is a Python SConscript loaded through `extra_scripts`.
It is named `.json` on purpose: PlatformIO Remote only syncs `platformio.ini`,
`src/`, `include/`, `lib/`, `data/` and the built binaries, and a `.py` helper
at the project root would not be transferred (`missing SConscript file`). The
pre-hook writes the project partition table to `$BUILD_DIR/hpn_partitions.csv`
and points `PARTITIONS_TABLE_CSV` at it. The post-hook replaces `UPLOADCMD`
with an explicit `0x10000` application offset, or appends `$SOURCE` to
`$UPLOADERFLAGS` for the filesystem image.

Consequence: remote uploads only write the application or filesystem image. A
device that has never been flashed (no bootloader or partition table) needs one
local `pio run -t upload` first. Keep the helper inside `src/` and keep the
`.json` extension unless PlatformIO Remote's sync rules change.

## Layout

```
firmware/
├── platformio.ini
├── docs/
│   ├── hhn-1.0.md            # HomeNet Heatpumps 1.0 carrier: pins, 74HC595, MUX
│   └── io-emulation.md       # emulation model, API, web integration
├── include/hpn/          # public contracts shared by all layers
│   ├── build_target.h    # compile-time MCU / core count
│   ├── board.h           # carrier profiles and output backends
│   ├── io_types.h        # logical types: TempRole, CurrentChannel, Relay
│   ├── io_backend.h      # real pins vs emulation seam
│   ├── sim.h             # emulation model and UI hooks
│   ├── config.h          # struct Config (plan 01 §1)
│   ├── config_store.h    # active/mutable config + NVS persistence
│   ├── state.h           # SystemState, enums, alarms (plan 01 §3-§4)
│   ├── tasks.h           # RTOS task set start/health
│   ├── l0_io.h           # L0 interface
│   ├── l1_safety.h       # L1 interface (bench loopback; production TODO)
│   ├── l2_control.h      # L2 interface (bench loopback; production TODO)
│   ├── l4_comms.h        # L4 interface (Wi-Fi + MQTT + portal)
│   ├── mqtt_client.h     # MQTT/HA discovery and command intake
│   ├── web_portal.h      # portal begin/tick/event-log interface
│   ├── wifi_client.h     # station-mode Wi-Fi client
│   └── logger.h          # logger interface (stub)
├── src/
│   ├── main.cpp          # boot sequence, carrier selection, RTOS start
│   ├── board.cpp         # HomeNet Heatpumps 1.0 and boardless definitions
│   ├── io_backend.cpp    # hardware or emulated I/O implementation
│   ├── sim.cpp           # emulation model (only under HPN_IO_EMULATION)
│   ├── config.cpp        # compiled-in safe defaults
│   ├── config_store.cpp  # NVS-backed configuration blob
│   ├── tasks.cpp         # task creation, priorities, core placement, WDT
│   ├── state.cpp         # boot-safe SystemState initialization
│   ├── l0_io.cpp         # carrier logic, the only layer that drives I/O
│   ├── l1_safety.cpp     # L1 bench loopback + heater interlock; TODO(L1)
│   ├── l2_control.cpp    # L2 bench pass-through; TODO(L2)
│   ├── l4_comms.cpp      # L4 integration (Wi-Fi + MQTT + portal)
│   ├── mqtt_client.cpp   # MQTT transport, HA discovery, command intake
│   ├── web_portal.cpp    # HTTP/WS server, API, logs, Test Mode, OTA
│   ├── remote_upload.json # remote upload SConscript (see above)
│   ├── wifi_client.cpp   # station-mode Wi-Fi client
│   └── logger.cpp        # logger stub
├── lib/                  # project-local libraries
├── test/                 # unit/native tests
└── data/                 # LittleFS portal assets
    ├── index.html        # main SPA shell
    ├── emulation.html    # bench-only carrier page
    ├── favicon.svg
    ├── css/style.css
    └── js/app.js, js/emulation.js
```

Precompressed `.gz` siblings live next to every asset. `src/remote_upload.json`
is a Python SConscript despite its extension; see the remote upload section.

## Safety notes for this scaffold

- Check the `[l0] backend` boot line. `esp32dev_sim` is emulated and must never
  be expected to drive real relays; `esp32s2` is hardware.
- `HPN_BENCH_LOOPBACK` is enabled only for `esp32dev_sim`. It is a wiring aid
  for the portal, not L1 safety arbitration. Never add it to a hardware build.
- The portal's Test Mode and configuration editor are not safety-rated. The
  external hardware over-temperature cutout and pump-cut switch remain
  mandatory and independent of this firmware.
- On HomeNet Heatpumps 1.0 the relay outputs and the analogue-MUX select lines
  share one 74HC595. Keep a single writer (`T_io`); the safe byte is `0x00`.
  See [`docs/hhn-1.0.md`](docs/hhn-1.0.md) before wiring anything.
- Unassigned pins use `HPN_GPIO_UNASSIGNED` and are never driven.
- Relay-driver polarity at the 74HC595 outputs is assumed **active-high**;
  confirm it before connecting pumps or the boiler contact.
- The external hardware over-temperature cutout and pump-cut switch (Layer 0)
  are mandatory and independent of this firmware.
- `HPN_HAS_DUAL_CORE` is 0 for ESP32-S2 and ESP32-C3. Do not call
  `xTaskCreatePinnedToCore(..., core 1)` on those parts; the task set collapses
  onto the single core.
- Configured defaults are placeholders. Safety limits must be finalized and
  bound-checked before any real actuator is connected.
- Flashing replaces whatever firmware currently runs the plant. Do it only with
  the pumps and boiler disabled or with explicit intent.
