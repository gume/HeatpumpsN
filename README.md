# HeatpumpsN

Multi-circuit heating and floor-mixer controller for an ESP32, plus the analysis
work that derives its control parameters.

The design is documented in [`plans/`](plans/). Start with
[`plans/master_plan.md`](plans/master_plan.md) for the system architecture and
[`plans/controller node/00_index_and_architecture.md`](plans/controller%20node/00_index_and_architecture.md)
for the firmware layers.

> **Continuing the project?** Read [`HANDOVER.md`](HANDOVER.md) first. It covers
> the current state at commit `bea823e`, remote-agent access, the build/flash/test
> workflow, a ten-minute architecture briefing, what is implemented vs missing,
> prioritized next steps and the local uncommitted work.

## Repository layout

| Path | Purpose |
| --- | --- |
| `HANDOVER.md` | Project handover: state, access, workflow, architecture, next steps |
| `firmware/` | PlatformIO firmware project (ESP32 family) |
| `firmware/HANDOVER.md` | Engineering log of the last firmware change set |
| `tests/` | Runtime integration checks against a running controller |
| `plans/` | System, controller-node, and observer specifications |
| `analysis/` | Python analysis, exported telemetry, and derived parameters |
| `others/` | Local read-only reference code from other developers (ignored by git) |

## Data and third-party code

These are intentionally **not tracked**:

- `analysis/heatpumps.duckdb` — 264 MB local analysis database
- `analysis/db/` — exported per-entity CSV files
- `others/` — third-party reference checkout (EMS-ESP32, with its own `.git`)

They stay on the development machine and can be regenerated or re-cloned.
See [`.gitignore`](.gitignore).

## Firmware quick start (remote)

The ESP32 is attached to a remote runner running a PlatformIO remote agent
(currently agent `EDI`, device `/dev/ttyUSB0`). Build, flash, and monitor
through the remote agent.

On the runner, start the agent if it is not already running:

```bash
pio remote agent start
```

On this host, authenticate once with your PlatformIO account and confirm the
agent is visible:

```bash
pio account login
pio remote agent list
```

Build and flash from the repository root:

```bash
# Build the default target (boardless devkit, emulated Heatpumps 1.0)
pio remote run -d firmware

# Build and flash
pio remote run -d firmware --target upload

# Inspect boot logs; always bound the monitor with a timeout
timeout 10s pio remote device monitor -d firmware -b 115200 || true
```

Never run `pio remote device monitor` without a timeout: it blocks the session.

Explicit targets:

```bash
pio remote run -d firmware -e esp32dev       # boardless, real GPIO, no carrier
pio remote run -d firmware -e esp32s2        # Heatpumps 1.0 hardware (S2 Mini)
pio remote run -d firmware -e esp32s3
pio remote run -d firmware -e esp32c3
```

See [`firmware/README.md`](firmware/README.md) for target details, the FreeRTOS
task set, and the current implementation status.

## Runtime model

The firmware runs a fixed FreeRTOS task set instead of a `loop()` scheduler:

| Task | Period | Dual-core affinity |
| --- | --- | --- |
| `T_safety` (L1) | 500 ms | Core 1 |
| `T_io` (L0) | 20 ms | Core 1 |
| `T_control` (L2) | 2000 ms | Core 1 |
| `T_comms` (L4) | 50 ms | Core 0 |
| `T_logger` | 1000 ms | Core 0 |

On single-core parts (ESP32-S2, ESP32-C3) every task runs on the single core and
nothing is pinned to a non-existent Core 1. `T_io` feeds the task watchdog only
while the real-time tasks are healthy, so a hung safety or control task resets
the controller rather than leaving outputs latched.

## Boards and I/O emulation

Two controller boards are supported:

| Board | Status | MCU |
| --- | --- | --- |
| HomeNet Heating control v0.1 | in the plant today | ESP32-S2 (LOLIN S2 Mini footprint) |
| HomeNet Heating control v1.x | current design, not yet validated on hardware | ESP32-C3 |

Board wiring is isolated in a compile-time profile
(`-DHPN_CARRIER_HHN1`, `-DHPN_CARRIER_HHN1X`, `-DHPN_CARRIER_BOARDLESS`). The
pin maps, 74HC595/PCF8574 bit maps, per-output polarity, fault input, current
sensing and safety caveats are documented in
[`firmware/docs/boards.md`](firmware/docs/boards.md).

The day-to-day development target is a **boardless ESP32 devkit** running the
v0.1 board **emulated** (`-DHPN_IO_EMULATION`, env `esp32dev_sim`, the default
PlatformIO environment): L0 drives an in-memory model of the carrier, mocked
temperatures and pump power can be edited, and no pin is touched. The planned
web dashboard gets a board-emulation page built on the same model. See
[`firmware/docs/io-emulation.md`](firmware/docs/io-emulation.md).

## Git

The default branch is `main`. Build output (`.pio/`), local data, secrets, and
IDE state are ignored. Commit source, plans, and analysis scripts.
