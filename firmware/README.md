# HeatpumpsN firmware

> **Project handover:** start at [`../HANDOVER.md`](../HANDOVER.md) for current
> state, remote-agent access, workflow, architecture and next steps.
> [`HANDOVER.md`](HANDOVER.md) is the engineering log of the last change set
> (commit `bea823e`).

PlatformIO project for the ESP32 controller node. It currently implements a
FreeRTOS task set, the boot-safe **L0** output layer, a board abstraction for
HomeNet Heating control v0.1 and v1.x, a compile-time **I/O emulation** backend so the
controller can be developed on a boardless devkit, a station-mode **Wi-Fi
client**, a **MQTT/Home Assistant** integration, and a LittleFS-backed **web
portal** in L4. L1 safety arbitration and L2 floor control are implemented;
L3 adaptation and the observer-owned tuning remain TODO. The next implementation
steps are described in
[`../plans/controller node/`](../plans/controller%20node/).

## Targets, boards and backends

| Environment | MCU board | Core | Board | Backend |
| --- | --- | --- | --- | --- |
| `esp32dev_sim` | `esp32dev` | dual-core Xtensa | HomeNet Heating control v0.1 | **emulated** — default env |
| `esp32dev_hhn1x_sim` | `esp32dev` | dual-core Xtensa | HomeNet Heating control v1.x | **emulated** bench |
| `esp32dev` | `esp32dev` | dual-core Xtensa | boardless | hardware (no pins assigned) |
| `esp32s2` | `lolin_s2_mini` | single-core Xtensa | HomeNet Heating control v0.1 | hardware, runtime output driver + MUX |
| `esp32c3_hhn1x` | `esp32-c3-devkitm-1` | single-core RISC-V | HomeNet Heating control v1.x | hardware, PCF8574 + ADS1115 |
| `esp32s3_hhn1x` | `esp32-s3-devkitc-1` | dual-core Xtensa | HomeNet Heating control v1.x | hardware, PCF8574 + ADS1115 |
| `esp32s3` | `esp32-s3-devkitc-1` | dual-core Xtensa | boardless | hardware (no pins assigned) |
| `esp32c3` | `esp32-c3-devkitm-1` | single-core RISC-V | boardless | hardware, future board |

Boards describe wiring; the backend describes whether writes reach pins. They
are independent compile-time choices:

- `-DHPN_CARRIER_HHN1` — HomeNet Heating control v0.1: 74HC595 default output
  driver, analogue current MUX, 5 LEDs, 1-Wire. ESP32-S2 in the field. The
  output driver itself is runtime configuration, so a PCF8574 can be selected
  on the same board.
- `-DHPN_CARRIER_HHN1X` — HomeNet Heating control v1.x: PCF8574 relay board,
  P7 interlock fault input, ADS1115 current sensing with an optional TLV9064
  assembly. No analogue MUX.
- `-DHPN_CARRIER_BOARDLESS` — bare devkit, pins unassigned
  (`HPN_GPIO_UNASSIGNED`) until a board layout is selected.
- `-DHPN_IO_EMULATION` — L0 runs against an in-memory model of the board
  instead of pins.

Both boards are documented in [`docs/boards.md`](docs/boards.md): pin maps,
74HC595/PCF8574 bit maps, per-output polarity, the v1.x fault input, MUX and
ADS1115 channels, current-sensor scaling, 1-Wire ROM IDs, LED mapping and the
safety caveats of each wiring.

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
| `T_logger` | 1000 ms | lowest | Core 0 | RAM event/slow circular buffers and diagnostic events |

Notes:

- On single-core parts (ESP32-S2, ESP32-C3, `HPN_HAS_DUAL_CORE == 0`) all tasks
  are created with `xTaskCreate`; none is pinned to a non-existent Core 1.
- Health supervision covers `T_safety`, `T_io` and `T_control` only. `T_comms`
  and `T_logger` are deliberately excluded so a network stall cannot trip the
  watchdog. AsyncTCP subscribes its own service task to the ESP task watchdog
  by default; the build sets `-DCONFIG_ASYNC_TCP_USE_WDT=0` to keep that
  guarantee, otherwise one slow HTTP handler (a large JSON response, OTA, weak
  Wi-Fi) leaves the shared watchdog entry stale and resets the controller.
- `T_io` subscribes to the task watchdog and feeds it only while the real-time
  tasks are within their deadlines. A hung safety or control task therefore
  resets the controller instead of leaving outputs latched. Each withheld feed
  is logged once per second with the supervised task ages, so a reset can be
  diagnosed from the captured boot log.
- Task bodies call the corresponding layer `tick()`. L0, the Wi-Fi client,
  MQTT/HA and the portal are implemented. L1 owns the safety supervision
  (limits, mixer window, pump/relay plausibility, role and DHW warnings) and L2
  owns floor/mixer coordination; L3 adaptation is still pending.

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
| `switch` | Heating loop 1 | `state/heating_loop_1` | `cmd/heating_loop_1` |
| `switch` | Heating loop 2 | `state/heating_loop_2` | `cmd/heating_loop_2` |
| `switch` | Floor heating | `state/floor_heating` | `cmd/floor_heating` |
| `binary_sensor` | Pump 1 | `state/pump_1_physical` | — |
| `binary_sensor` | Pump 2 | `state/pump_2_physical` | — |
| `binary_sensor` | Floor pump | `state/floor_pump_physical` | — |
| `binary_sensor` | Heater | `state/heater_physical` | — |
| `sensor` | Floor pump power | `state/power_floor` | — |
| `sensor` | Radiator 1 pump power | `state/power_rad1` | — |
| `sensor` | Radiator 2 pump power | `state/power_rad2` | — |
| `sensor` | Mixer motor power | `state/power_mixer` | — |
| `sensor` | Mixer state (`idle`/`opening`/`closing`) | `state/mixer_state` | — |
| `sensor` | Boiler supply temperature | `state/temp_boiler` | — |
| `sensor` | Floor return temperature | `state/temp_return` | — |
| `sensor` | Mixer forward temperature | `state/temp_mix` | — |
| `number` | Floor setpoint | `state/floor_setpoint` | `cmd/floor_setpoint` |
| `sensor` | System error code (`0` = no error) | `state/system_error_code` | — |
| `sensor` | System error text | `state/system_error_text` | — |

The three switches are the only HA command path for heating. Their state
topics carry the **effective per-circuit demand** after the keep-alive check;
the four binary sensors carry the true L1/L0 physical output state. There are
no virtual/duplicate entities. In normal operation L2 derives the shared heater
call from the three circuit demands (`heater_requested = floor || loop 1 ||
loop 2`); Test Mode is the only way to drive the heater manually. Each accepted
switch command resets that circuit's `demand_age_s`, so a repeated `ON` is the
keep-alive. A missed keep-alive drops the demand (error `301`) but keeps the raw
intent, and a later `ON` re-arms it. When a demand ends, the board keeps that
circuit's pump running for `pump_stop_after_heating_s` (pump-only after-run)
before stopping it.

The retired `heater` / `pump_*` switches and the old virtual/physical binary
sensors are deleted from HA by publishing empty retained discovery payloads on
every discovery cycle, so upgrades do not leave stale entities behind.

The floor setpoint command updates `Config::floor_setpoint_shadow`; it is an
HA-owned runtime shadow, not a Category 1 configuration value. MQTT commands set
`*_intended` and reset the circuit demand age, so on the bench they flow through
L2 (Test Mode passes them through directly) and L1's safety arbitration before
reaching the carrier.

State and discovery are retained. The client reconnects every 5 s while the
broker is unreachable, and the portal reports `mqtt.state`, broker, connect
counts and error count through `/api/status` and the live WebSocket snapshot.
`POST /api/mqtt/test` queues a non-retained test publication; it returns `202`
when the broker is not connected yet and `409` when no broker host is set.

The two diagnostic sensors are published only when `system_error_code` or
`active_alarms` changes. `system_error_code` carries the standardized numeric
code (`0` when there is no error) and `system_error_text` carries the readable
message plus active warning/alarm flags (`No error` when healthy). Ordinary
relay, pump, mixer and setpoint transitions do not update them.

### Telemetry log streaming

`hpn::logger` owns the event and slow logs in RAM: 96 event records and 180 slow
samples at a 10 s cadence. Nothing is written to flash. The same buffers feed
the portal's `/api/log/events` and `/api/log/slow` endpoints.

Streaming to MQTT is opt-in via `mqtt_log_enabled` on **Settings → Network &
MQTT**. New records are published as JSON lines to `<base>/log/event` and
`<base>/log/slow`; empty topic fields use those base-derived defaults, while a
non-empty `mqtt_log_event_topic` / `mqtt_log_slow_topic` replaces the full topic.
Publishes are non-retained. If the broker is unreachable, logging continues in
RAM and the retained backlog is sent after reconnection.

## Relay counter persistence

Relay cycles and runtime live in the portal's RAM cache. In hardware builds
(`!HPN_IO_EMULATION`) they are also stored in the separate `hpnctr` NVS
namespace:

- the blob carries a magic value, format version, exact struct size and a
  CRC32 checksum; a CRC failure in the current format resets the counters and
  latches `WARN_NVS_CORRUPTED` / `ERR_NVS_DATA_CORRUPTED`, while an older format
  is discarded silently;
- counters are loaded at boot;
- a dirty flag is set by a cycle change or by the one-second runtime
  accumulation;
- the cache is written at most once per hour, and only when dirty;
- an explicit counter reset requests an immediate save so a reboot cannot
  resurrect the old values;
- emulation builds never load or save counters, so the bench model cannot wear
  the NVS or leak test data into a hardware configuration.

The anti-seize schedule (`hpnanti`) uses the same CRC32-protected blob pattern.

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
  heater/safety state (including the error code and readable error text), the
  five carrier status LEDs and recent events.
- **Plant** — read-only circuits, pumps, after-run timers and mixer feedback.
- **Diagnostics** — Event Log, Slow Log (10 s samples), Sensor Health, Power
  Meters and Relay Counters.
- **Test Mode** — time-limited manual control through the same
  `*_intended` → `*_requested` intake as HA. The heater is refused unless a pump
  is commanded ON. Activation/deactivation and every command are logged.
- **Settings** — Safety & Timeouts, Hardware & Pins, Network & MQTT, Firmware
  Update and System. Hardware & Pins selects the output driver (direct GPIO,
  74HC595 or PCF8574), its wiring/bit map and active-high/low polarity before
  showing the remaining sensor and calibration fields. Saves are validated
  server-side and written to NVS only when the form is submitted. The Network
  page shows the live MQTT state, can queue a test publication, and configures
  optional log streaming.
- **Runtime Parameters** — read-only HA/observer values.

The event, access and slow logs are RAM-only in this build; they reset on
reboot. Event and slow records can optionally be mirrored to MQTT (see above).
Relay counters are persisted hourly in hardware builds and stay in RAM under
emulation (see above). Configuration, counters and the anti-seize schedule are
persisted as CRC32-protected NVS blobs. The config blob is versioned: v1 and
v2 blobs are migrated to v3 by copying their unchanged prefix over current
defaults, preserving every existing setting while initialising the appended
fields, and the upgraded blob is saved immediately. Counters and the anti-seize
schedule still have no migration path; an old-format blob of those is
discarded. The
`WARN_NVS_CORRUPTED` / `ERR_NVS_DATA_CORRUPTED` latch is held for the current
boot: saving settings rewrites the config blob, but the live flag clears only
on the next boot once every persisted blob validates. This keeps unrelated
counter or anti-seize damage visible instead of hiding it behind a config save.
The access log records every portal request plus WebSocket connect/disconnect
events.

### Status LEDs

L4's `hpn::led::tick` (`src/led_status.cpp`) animates the five addressable
LEDs through L0's `set_leds`:

- **LED 0** system/comms: green when online, yellow breathing while Wi-Fi or
  MQTT is down, red flashing on a critical alarm;
- **LED 1** boiler: orange while the heater is on, cyan for DHW priority,
  purple blinking during anti-short-cycle hold-off;
- **LED 2** floor: green while the floor pump runs, cyan breathing during a
  warm-up flush, magenta on ΔT collapse, red rapid blink on over-temperature;
- **LEDs 3–4** radiators: orange while the pump runs, red on a welded-relay or
  dry-run fault.

### Test Mode and the bench loopback

`HPN_BENCH_LOOPBACK` is compiled only into `esp32dev_sim`. In Test Mode L2
copies `*_intended` to `*_requested`; L1 still applies the complete safety set
(over-temperature, gradient, anti-short-cycle, pump/relay plausibility, mixer
window and sensor supervision) and drives the relays. The loopback is a bench
aid for exercising the portal and the output path, **not** a bypass of safety
arbitration, and it never appears in a hardware build.

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
pio remote run -e esp32dev_sim                # daily dev: emulated HomeNet Heating control v0.1
pio remote run -e esp32dev_hhn1x_sim          # emulated HomeNet Heating control v1.x
pio remote run -e esp32s2                     # real HomeNet Heating control v0.1
pio remote run -e esp32c3_hhn1x               # real HomeNet Heating control v1.x
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

## Runtime verification

`tests/runtime_check.py` is a repeatable integration check against a running
controller. It prints `PASSED` on success and covers the HTTP API, the RAM log
rings, the versioned config contract, MQTT discovery/state topics and log
streaming, HA error-sensor transitions, the five-LED mapping, the L1 mixer
opening ceiling, sensor-role verification and (with `--serial`) the boot log.

```bash
python3 tests/runtime_check.py --base-url http://192.168.2.14
tests/flash_and_check.sh --serial        # build + flash esp32dev_sim + checks
```

Verified on the bench ESP32 (`esp32dev_sim`, 2026-10-27):
`PASSED: 89 passed, 0 failed, 0 skipped` with `--serial`, and `82/82` without
the reboot. The boot log showed the saved config loading, the 74HC595 driver
with its pin map and `/OE` polarity, no Preferences errors, no `501` latch and
no crash markers. The serial run also round-tripped a PCF8574 driver selection
and rejected duplicate expander bits.

Requirements, the full check list and the state the suite restores are in
[`../tests/README.md`](../tests/README.md). `--serial` reboots the board.

## Layout

```
firmware/
├── platformio.ini
├── docs/
│   ├── boards.md             # board descriptions: pins, output drivers, fault input, current sensing
│   └── io-emulation.md       # emulation model, API, web integration
├── include/hpn/          # public contracts shared by all layers
│   ├── build_target.h    # compile-time MCU / core count
│   ├── board.h           # board profiles and output backends
│   ├── io_types.h        # logical types: TempRole, CurrentChannel, Relay
│   ├── io_backend.h      # real pins vs emulation seam
│   ├── sim.h             # emulation model and UI hooks
│   ├── config.h          # struct Config (plan 01 §1)
│   ├── config_store.h    # NVS-backed config load/save (CRC32, versioned, v1/v2/v3→v4)
│   ├── crc32.h           # portable CRC32 helper
│   ├── state.h           # SystemState, enums, alarms (plan 01 §3-§4)
│   ├── tasks.h           # RTOS task set start/health
│   ├── l0_io.h           # L0 interface
│   ├── l1_safety.h       # L1 interface (arbitration, limits, mixer servo)
│   ├── l2_control.h      # L2 interface (coordination, mixer target)
│   ├── l4_comms.h        # L4 interface (Wi-Fi + MQTT + portal + LEDs)
│   ├── led_status.h      # five-LED status animator
│   ├── mqtt_client.h     # MQTT/HA discovery and command intake
│   ├── web_portal.h      # portal begin/tick/event-log interface
│   ├── wifi_client.h     # station-mode Wi-Fi client
│   └── logger.h          # RAM event/slow buffers + MQTT stream API
├── src/
│   ├── main.cpp          # boot sequence, carrier selection, RTOS start
│   ├── board.cpp         # HomeNet Heating control v0.1/v1.x and boardless profiles
│   ├── io_backend.cpp    # hardware or emulated I/O implementation
│   ├── sim.cpp           # emulation model (only under HPN_IO_EMULATION)
│   ├── config.cpp        # compiled-in safe defaults
│   ├── config_store.cpp  # CRC32-protected NVS configuration blob
│   ├── tasks.cpp         # task creation, priorities, core placement, WDT
│   ├── state.cpp         # boot-safe SystemState and diagnostic text helpers
│   ├── l0_io.cpp         # carrier logic, the only layer that drives I/O
│   ├── l1_safety.cpp     # L1 arbitration, limits, mixer servo, pump supervision
│   ├── l2_control.cpp    # L2 coordination, PI + Smith predictor, DHW/flush
│   ├── l4_comms.cpp      # L4 integration (Wi-Fi + MQTT + portal + LEDs)
│   ├── led_status.cpp    # status animation mapped to the five LEDs
│   ├── mqtt_client.cpp   # MQTT transport, HA discovery, command intake
│   ├── web_portal.cpp    # HTTP/WS server, API, logs, Test Mode, OTA
│   ├── remote_upload.json # remote upload SConscript (see above)
│   ├── wifi_client.cpp   # station-mode Wi-Fi client
│   └── logger.cpp        # RAM ring buffers and diagnostic event generation
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
- The mixer opening is capped by `safety.mix_max_open_pct` (default **85 %**).
  L1 refuses to open past it on every move, including maintenance sweeps; L2
  and the portal clamp their targets to it. Closing and end-stop homing are
  unaffected. An optional two-point dynamic maximum can lower the ceiling above
  a boiler-temperature switch point, and the effective ceiling is always the
  minimum of the absolute and dynamic limits.
- `safety.mix_min_open_pct` (default **40 %**) is a normal floor-demand minimum
  only. It never blocks closing, safety close, homing, DHW or a no-demand close.
- `pump_power_check_enabled` gates dry-run and welded-relay detection. Leave it
  off until the shared current calibration (`pump_current_w_per_v`,
  `pump_current_offset_v`) has been checked with a clamp meter; measurements
  stay visible either way. It is also inert when the configured front end cannot
  measure at all (no current sensing selected, or a v1.x ADS1115 build with
  `tlv9064_present` false), so a missing sensor is never read as dry pumps.
- Each of the four current channels is optional and enabled separately in
  **Settings → Hardware → Power meters** (`has_pump_power[]` for floor,
  radiator 1 and radiator 2, `has_mixer_power` for the mixer). A disabled meter
  is not sampled, reports 0 W and is ignored by protection; the Diagnostics page
  shows all four readings with their fitted state.
- Mixer anomalies (motion longer than a full stroke, or no encoder response
  during a commanded move) and sensor-role/DHW inference problems are
  **warning-only**. They do not stop the plant; L1's hard over-temperature rule
  remains authoritative.
- `HPN_BENCH_LOOPBACK` is enabled only for `esp32dev_sim`. It is a wiring aid
  for the portal, not L1 safety arbitration. Never add it to a hardware build.
- The portal's Test Mode and configuration editor are not safety-rated. The
  external hardware over-temperature cutout and pump-cut switch remain
  mandatory and independent of this firmware.
- The output driver is runtime configuration: direct GPIO, 74HC595
  (HomeNet Heating control v0.1) or PCF8574 (HomeNet Heating control v1.x).
  On v0.1 the relay outputs and the analogue-MUX select lines share one serial
  output extender (not a bare 74HC595); the default bit map and `0x00` safe
  byte live in `board_apply_defaults()` and the Hardware & Pins page. Keep a
  single writer (`T_io`). See [`docs/boards.md`](docs/boards.md) before wiring
  anything.
- Unassigned pins use `HPN_GPIO_UNASSIGNED` and are never driven.
- The v0.1 output extender pulls its selected outputs to GND at
  power-on/reset and the relay board is active-high, so `0x00` is its safe
  state. A v1.x PCF8574 idles high and its relay board is active-low, so set
  the per-output active-low flags and use the `0xFF` safe byte. Re-check the
  polarity after any board swap; it is a safety-relevant setting.
- The external hardware over-temperature cutout and pump-cut switch (Layer 0)
  are mandatory and independent of this firmware.
- `HPN_HAS_DUAL_CORE` is 0 for ESP32-S2 and ESP32-C3. Do not call
  `xTaskCreatePinnedToCore(..., core 1)` on those parts; the task set collapses
  onto the single core.
- Configured defaults are placeholders. Safety limits must be finalized and
  bound-checked before any real actuator is connected.
- Flashing replaces whatever firmware currently runs the plant. Do it only with
  the pumps and boiler disabled or with explicit intent.
