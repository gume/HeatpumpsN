# HeatpumpsN firmware

PlatformIO project for the ESP32 controller node. The scaffold currently
implements the boot-safe **L0** output initialization; L1 safety arbitration,
L2 control, L3 adaptation, and L4 communications are the next implementation
steps described in [`../plans/controller node/`](../plans/controller%20node/).

## Targets

| Environment | Board | Core | Notes |
| --- | --- | --- | --- |
| `esp32dev` | `esp32dev` | dual-core Xtensa | Default build target |
| `esp32s3` | `esp32-s3-devkitc-1` | dual-core Xtensa | More RAM/GPIO |
| `esp32s2` | `lolin_s2_mini` | single-core Xtensa | Current physical board |
| `esp32c3` | `esp32-c3-devkitm-1` | single-core RISC-V | Limited GPIO, no dual-core task pinning |

`esp32s2` uses the LOLIN S2 Mini board definition. Change it to
`lolin_s2_pico` in `platformio.ini` if the physical board is a PICO.

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
├── include/hpn/          # public contracts shared by all layers
│   ├── build_target.h    # compile-time MCU / core count
│   ├── config.h          # struct Config (plan 01 §1)
│   ├── state.h           # SystemState, enums, alarms (plan 01 §3-§4)
│   └── l0_io.h           # L0 interface
├── src/
│   ├── main.cpp          # boot sequence + placeholder scheduler
│   ├── config.cpp        # compiled-in safe defaults
│   ├── state.cpp         # boot-safe SystemState initialization
│   └── l0_io.cpp         # the only code that touches GPIOs
├── lib/                  # project-local libraries
├── test/                 # unit/native tests
└── data/                 # LittleFS contents for the web dashboard
```

## Safety notes for this scaffold

- Most GPIO assignments are still **TBD** in the plan. Unassigned pins use
  `HPN_GPIO_UNASSIGNED` and are never driven.
- Relays are assumed **active-high**; confirm the output-board polarity before
  connecting pumps or the boiler contact.
- The external hardware over-temperature cutout and pump-cut switch (Layer 0)
  are mandatory and independent of this firmware.
- `HPN_HAS_DUAL_CORE` is 0 for ESP32-S2 and ESP32-C3. Do not call
  `xTaskCreatePinnedToCore(..., core 1)` on those parts; the planned task map
  must collapse onto the single core.
- Configured defaults are placeholders. Safety limits must be finalized and
  bound-checked before any real actuator is connected.
