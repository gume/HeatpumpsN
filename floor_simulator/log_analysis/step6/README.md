# Step 6 - Integrated HIL plant simulator and validation

**Final:** v1.0 - `step6_plant_validation_report_v1.md` (8/8 gates PASS).

| File | Role |
|---|---|
| `plant_v1.py` | Reference plant model (numba), component/coupled modes, legacy-controller replica |
| `plant_v1.hpp` | C++ port (`hpn_plant::Plant`), coupled mode, for the ESP32 emulation backend / host bench |
| `plant_v1_harness.cpp` | Parity harness |
| `step6_plant_validation_v1.py` | Consistency, coupled validation, closed loop, parity, boiler statistics, report |
| `step6_params_v1.json`, `gates_step6.json` | Integration constants, emulator statistics, results; frozen gates |
