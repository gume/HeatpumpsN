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
pio remote run -d firmware -e esp32s2   # current physical board: LOLIN S2 Mini
pio remote run -d firmware -e esp32c3
```

See [`firmware/README.md`](firmware/README.md) for target details and the
current scaffold status.

## Git

The default branch is `main`. Build output (`.pio/`), local data, secrets, and
IDE state are ignored. Commit source, plans, and analysis scripts.
