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

## Commands

```bash
pio run                    # default target (esp32dev)
pio run -e esp32s2         # build a specific target
pio run -t upload          # upload default target
pio run -e esp32s2 -t upload
pio device monitor         # 115200 baud, exception decoder enabled
pio run -t clean
```

To add dashboard assets (LittleFS), place files under `data/` and run
`pio run -t buildfs`.

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
