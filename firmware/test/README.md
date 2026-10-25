# Tests

Unit and native tests live here. PlatformIO discovers `test/test_*/test_*.cpp`.

Planned first tests:

- `config_defaults()` respects safety bounds.
- Alarm/error-code contract matches plan 01.
- Mixer dead-reckoning and pulse clamping (L0 state machine).
- L1 arbitration invariants: outputs off on boot, sensor-fault postures.
