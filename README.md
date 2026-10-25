# HeatpumpsN

Multi-circuit heating and floor-mixer controller for an ESP32, plus the analysis
work that derives its control parameters.

The design is documented in [`plans/`](plans/). Start with
[`plans/master_plan.md`](plans/master_plan.md) for the system architecture and
[`plans/controller node/00_index_and_architecture.md`](plans/controller%20node/00_index_and_architecture.md)
for the firmware layers.

## Repository layout

| Path | Purpose |
| --- | --- |
| `firmware/` | PlatformIO firmware project (ESP32 family) |
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
# Build the default target (classic ESP32 DevKit)
pio remote run -d firmware

# Build and flash
pio remote run -d firmware --target upload

# Inspect boot logs; always bound the monitor with a timeout
timeout 10s pio remote device monitor -d firmware -b 115200 || true
```

Never run `pio remote device monitor` without a timeout: it blocks the session.

Additional targets:

```bash
pio remote run -d firmware -e esp32s3
pio remote run -d firmware -e esp32s2   # legacy carrier: LOLIN S2 Mini
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

## Board profiles

Hardware wiring is isolated in a board profile selected at compile time:

| Profile | Selected by | Backend |
| --- | --- | --- |
| `old-lolin-s2` | `esp32s2` (`-DHPN_BOARD_OLD_LOLIN_S2`) | 74HC595 + analogue MUX |
| `generic-devkit` | `esp32dev`, `esp32s3`, `esp32c3` | direct GPIO |

The legacy LOLIN S2 Mini carrier is the temporary physical board while the new
one is being designed. Its pin map, 74HC595 bit map, MUX channels, current-sensor
scaling and safety caveats are documented in
[`firmware/docs/board-old-lolin-s2.md`](firmware/docs/board-old-lolin-s2.md).
The new board will get its own profile; the control logic stays untouched.

## Git

The default branch is `main`. Build output (`.pio/`), local data, secrets, and
IDE state are ignored. Commit source, plans, and analysis scripts.
