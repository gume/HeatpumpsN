# HeatpumpsN firmware

PlatformIO project for the ESP32 controller node. It currently implements a
FreeRTOS task set, the boot-safe **L0** output layer and a board abstraction
with two profiles. L1 safety arbitration, L2 control, L3 adaptation, and L4
communications are idle stubs and the next implementation steps described in
[`../plans/controller node/`](../plans/controller%20node/).

## Targets and board profiles

| Environment | Board | Core | Board profile | Notes |
| --- | --- | --- | --- | --- |
| `esp32dev` | `esp32dev` | dual-core Xtensa | `generic-devkit` | Default build target |
| `esp32s3` | `esp32-s3-devkitc-1` | dual-core Xtensa | `generic-devkit` | More RAM/GPIO |
| `esp32s2` | `lolin_s2_mini` | single-core Xtensa | `old-lolin-s2` | Legacy carrier, current physical board |
| `esp32c3` | `esp32-c3-devkitm-1` | single-core RISC-V | `generic-devkit` | Limited GPIO, single core |

The board profile is selected at compile time with `-DHPN_BOARD_*` and chooses
the output backend (direct GPIO or 74HC595), pin/bit assignment, sensor ROM
IDs, LED count and mixer travel time. The generic profile leaves pins
unassigned (`HPN_GPIO_UNASSIGNED`) until the new board is defined.

The legacy LOLIN S2 Mini carrier is documented in
[`docs/board-old-lolin-s2.md`](docs/board-old-lolin-s2.md): pin map, 74HC595 bit
map, MUX channels, ACS712 scaling, 1-Wire ROM IDs, LED mapping and the safety
caveats of the old wiring. `esp32s2` uses the `lolin_s2_mini` board definition;
switch it to `lolin_s2_pico` in `platformio.ini` if the physical board is a
PICO.

## RTOS task set

`hpn::tasks::start()` creates five permanent tasks and then deletes the Arduino
`loopTask`, so only the planned tasks remain.

| Task | Period | Priority | Dual-core affinity | Purpose |
| --- | --- | --- | --- | --- |
| `T_safety` | 500 ms | highest | Core 1 | L1 safety arbitration |
| `T_io` | 20 ms | high | Core 1 | L0 hardware access, watchdog feeder |
| `T_control` | `config.tick_ms` (default 2000 ms) | medium | Core 1 | L2 control and coordination |
| `T_comms` | 50 ms | low | Core 0 | L4 MQTT / HTTP / commands |
| `T_logger` | 1000 ms | lowest | Core 0 | Event log and NVS persistence |

Notes:

- On single-core parts (ESP32-S2, ESP32-C3, `HPN_HAS_DUAL_CORE == 0`) all tasks
  are created with `xTaskCreate`; none is pinned to a non-existent Core 1.
- Health supervision covers `T_safety`, `T_io` and `T_control` only. `T_comms`
  and `T_logger` are deliberately excluded so a network stall cannot trip the
  watchdog.
- `T_io` subscribes to the task watchdog and feeds it only while the real-time
  tasks are within their deadlines. A hung safety or control task therefore
  resets the controller instead of leaving outputs latched.
- Task bodies are minimal stubs calling the corresponding layer `tick()`.

## Commands (remote agent)

The target board is attached to a remote runner. Run these from this
directory (or add `-d firmware` from the repository root):

```bash
pio remote run                              # build default target (esp32dev)
pio remote run -e esp32s2                   # build a specific target
pio remote run --target upload              # build + flash
pio remote run -e esp32s2 --target upload
timeout 10s pio remote device monitor -b 115200 || true
```

Always bound `pio remote device monitor` with `timeout`; an unbounded monitor
blocks the session.

The remote agent is authenticated through the PlatformIO account
(`pio account login`). Check availability with `pio remote agent list`, and
start it on the runner with `pio remote agent start`.

Local `pio run` remains available as an offline compile-only fallback, but
build/flash/monitor operations for the target board use the remote agent.

## Layout

```
firmware/
├── platformio.ini
├── docs/
│   └── board-old-lolin-s2.md  # legacy carrier: pins, 74HC595, MUX, LEDs
├── include/hpn/          # public contracts shared by all layers
│   ├── build_target.h    # compile-time MCU / core count
│   ├── board.h           # board profiles and output backends
│   ├── config.h          # struct Config (plan 01 §1)
│   ├── state.h           # SystemState, enums, alarms (plan 01 §3-§4)
│   ├── tasks.h           # RTOS task set start/health
│   ├── l0_io.h           # L0 interface
│   ├── l1_safety.h       # L1 interface (stub)
│   ├── l2_control.h      # L2 interface (stub)
│   ├── l4_comms.h        # L4 interface (stub)
│   └── logger.h          # logger interface (stub)
├── src/
│   ├── main.cpp          # boot sequence, board selection, RTOS start
│   ├── board.cpp         # old LOLIN S2 and generic board definitions
│   ├── config.cpp        # compiled-in safe defaults
│   ├── tasks.cpp         # task creation, priorities, core placement, WDT
│   ├── state.cpp         # boot-safe SystemState initialization
│   ├── l0_io.cpp         # the only code that touches GPIOs
│   ├── l1_safety.cpp     # L1 stub
│   ├── l2_control.cpp    # L2 stub
│   ├── l4_comms.cpp      # L4 stub
│   └── logger.cpp        # logger stub
├── lib/                  # project-local libraries
├── test/                 # unit/native tests
└── data/                 # LittleFS contents for the web dashboard
```

## Safety notes for this scaffold

- The active board is the **legacy** carrier unless `HPN_BOARD_GENERIC_DEVKIT`
  is selected. See [`docs/board-old-lolin-s2.md`](docs/board-old-lolin-s2.md)
  before wiring anything to it.
- On the legacy carrier the relay outputs and the analogue-MUX select lines
  share one 74HC595. Keep a single writer (`T_io`); the safe byte is `0x00`.
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
