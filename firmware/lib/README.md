# Project-local libraries

Put private libraries here (`lib/<name>/`). Public dependencies are declared in
`platformio.ini` under `lib_deps`; PlatformIO will not use this directory for
those.

Planned local modules follow the controller layers:

- `l1_safety/` — deterministic safety arbitration
- `l2_control/` — mixer loop, coordination, flush state machine
- `l3_adaptive/` — observer parameter validation
- `l4_comms/` — MQTT/HA, web dashboard, logging
