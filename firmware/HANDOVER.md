# Firmware change log — board-managed heater (commits `11e8dc9`, `bd2b3e1`)

**Date:** 2026-10-27
**Base commit:** `35409ee` ("docs: add project handover")
**Firmware commits:** `11e8dc9` ("firmware: board-managed heater with per-circuit
keep-alive") and `bd2b3e1` ("portal: show heating loop demand, pumps and board
after-run")
**Tests/docs:** the accompanying change set updates `tests/` and the plans; it
does not change runtime behaviour.
**Firmware version string:** `0.3.0-web` (bumped from `0.2.0-web` to mark the
new HA/control contract)
**Device:** the local `esp32dev_sim` build succeeds. This change set has **not
yet been flashed or run on the remote ESP32** because the bench agent was not
available; run `tests/flash_and_check.sh` before treating the runtime behaviour
as verified.

> **Scope.** This is the engineering log for one change set, not the project
> handover. Start at [`../HANDOVER.md`](../HANDOVER.md) for current state, access,
> workflow, architecture and next steps; come back here for the details of this
> change.

> **Summary.** Home Assistant no longer owns the shared heater or the pump
> after-run. It sees exactly three heating-loop switches (`Heating loop 1`,
> `Heating loop 2`, `Floor heating`) and four physical binary sensors (`Pump 1`,
> `Pump 2`, `Floor pump`, `Heater`). Each switch is kept alive by repeating its
> `ON` command. The controller keeps a per-circuit demand age, drops only the
> effective demand when a keep-alive is missed (error `301`), derives the heater
> call from the three demands, and owns a per-circuit pump after-run. The portal,
> MQTT discovery, plans and runtime checks were updated with the same contract.

---

## 1. Objective

The previous firmware exposed a heater switch and orchestrated the after-run in
Home Assistant. That split had two problems:

1. HA had to know the heater interlock and after-run timing, duplicating
   controller policy and making the two sides drift.
2. A single heater keep-alive could not tell which circuit had gone quiet, and
   the old `302` after-heating timeout treated normal pump run-on as an error.

This session moves both responsibilities into the board while keeping HA as the
comfort authority: HA requests heat per circuit by switch, repeats the command as
a keep-alive, and the board decides the heater call and residual-heat run-on.

---

## 2. What changed

### 2.1 State contract

`firmware/include/hpn/state.h`:

* adds `CircuitIndex` (`CIRCUIT_FLOOR`, `CIRCUIT_RAD1`, `CIRCUIT_RAD2`,
  `CIRCUIT_COUNT`);
* adds `pump_*_demand` alongside `pump_*_intended`, `pump_*_requested` and
  `pump_*_phys`;
* replaces the global `keepalive_age_s` / `after_heating_timer_s` with
  `demand_age_s[CIRCUIT_COUNT]` and `after_run_age_s[CIRCUIT_COUNT]`;
* replaces `DIV_AFTER_HEATING_TIMEOUT` with `DIV_AFTER_RUN` and removes
  `ERR_AFTER_HEAT_TIMEOUT` (`302`).

`heater_intended` is still present but is now derived by L2 in normal mode. Test
Mode keeps manual heater control for bench work.

### 2.2 L2 keep-alive, heater derivation and after-run

`firmware/src/l2_control.cpp`:

* `update_demand_age()` increments each circuit's age only while its intent is
  set. A repeated `ON` resets that age; when it reaches `demand_timeout_s` the
  effective demand drops. The raw intent stays set, so the next `ON` re-arms it.
  Any stale demand sets `DIV_DEMAND_LOSS` and latches `ERR_HA_KEEPALIVE_TIMEOUT`
  (`301`); the error clears on the next tick without stale demand.
* In normal mode the heater call is `req_floor || req_rad1 || req_rad2` after
  the coordination rules and **before** the after-run.
* `service_after_run()` keeps a circuit's pump requested for
  `pump_stop_after_heating_s` after its demand ends, then stops it. The after-run
  is per circuit and never keeps the heater on.
* The mixer, flush, ΔT-collapse, DHW and `long_pause_s` rules still run before the
  heater/after-run decision. Test Mode bypasses the keep-alive and after-run and
  passes intents straight through.

### 2.3 MQTT / Home Assistant contract

`firmware/src/mqtt_client.cpp`:

* publishes three switches: `heating_loop_1`, `heating_loop_2`,
  `floor_heating`, with state topics
  `<base>/state/heating_loop_{1,2}` and `<base>/state/floor_heating`;
* publishes four physical binary sensors: `pump_1`, `pump_2`, `floor_pump`,
  `heater` on `<base>/state/pump_{1,2}_physical`,
  `<base>/state/floor_pump_physical`, `<base>/state/heater_physical`;
* subscribes to `<base>/cmd/heating_loop_1`, `cmd/heating_loop_2`,
  `cmd/floor_heating`, and resets that circuit's `demand_age_s` on **every**
  accepted command (so a repeated `ON` is the keep-alive);
* removes the retired heater/pump switches and virtual/physical binary sensors by
  publishing empty retained discovery payloads;
* no longer accepts `cmd/heater`.

Switch state publishes the effective demand, not the physical relay. The
physical sensors are the only pump/heater state entities.

### 2.4 Portal

`firmware/src/web_portal.cpp` and `firmware/data/js/app.js` (+ regenerated
`app.js.gz`):

* `/api/state` circuits now expose `intended`, `demand`, `cmd` (compatibility),
  `phys`, `demandAge` and the remaining `afterRun`;
* the heater block exposes `automatic`, `cmd`, `requested`, `phys` and a reason;
* `/api/sim/intent` accepts `heating_loop_1`, `heating_loop_2`,
  `floor_heating`, `dhw_active` and `keepalive`;
* the dashboard shows per-circuit demand, pump and after-run, and the heater as
  board-managed;
* Help/config labels describe the board-owned after-run.

### 2.5 Runtime checks and docs

* `tests/runtime_check.py` uses the new switch names, disables after-run for the
  relay/LED checks, verifies the three-switch/four-sensor discovery contract,
  adds a keep-alive `301` test and a board after-run test, and drives the Test
  Mode heater through `/api/test/command`.
* `tests/README.md`, `plans/master_plan.md`, `plans/mqtt_messaging_plan.md` and
  the controller-node plans (`00`, `01`, `03`, `04`, `05`, `06`, web UI) now
  describe the board-managed contract and the new divergence/error names.

---

## 3. Design decisions / caveats

* **HA still owns comfort.** It decides *when* a circuit needs heat; the board
  decides the shared heater call and residual-heat run-on.
* **Intended vs. demand.** A missed keep-alive clears the demand, not the last
  accepted command. This lets HA recover with a single repeated `ON` and makes
  the mismatch visible as `ERROR 301`.
* **The heater is not kept alive separately.** Its liveness follows the three
  circuit demands; there is no fourth timer.
* **The after-run is normal, not an error.** `DIV_AFTER_RUN` is informational and
  has no error code; `302` is retired.
* **After-run is pump-only.** The heater request is decided before the after-run,
  so residual heat is circulated with the burner call off.
* **Test Mode is still the manual bench path.** It keeps direct heater control
  and bypasses the keep-alive/after-run logic.
* **Discovery cleanup is automatic.** Upgrading an existing HA installation does
  not require deleting the old entities by hand.

---

## 4. Verification performed

Verified on the bench board (`esp32dev_sim`, 2026-10-27):

* remote flash of the firmware and the LittleFS image
  (`HPN_UPLOAD_FS=1 tests/flash_and_check.sh`);
* `python3 tests/runtime_check.py --serial` → **PASSED: 85 passed, 0 failed,
  0 skipped**, including the reboot/boot-log checks;
* the same suite without `--serial` → **PASSED: 78 passed, 0 failed,
  0 skipped**.

Local checks:

* `pio run -d firmware -e esp32dev_sim` → **SUCCESS**.
* `pio run -d firmware -e esp32s2` → **SUCCESS** (compile only, not flashed).
* `python3 -m py_compile tests/runtime_check.py` → **OK**.
* `node --check firmware/data/js/app.js` → **OK**.
* `gzip -9 -c firmware/data/js/app.js > firmware/data/js/app.js.gz` → regenerated.

The first `--serial` attempt hit a transient HTTP timeout in the LED checks;
an immediate rerun passed every check, so treat a single timeout as retryable
before assuming a regression.

The runtime suite now covers, in addition to the existing checks:

* exactly three switches and four physical binary sensors in HA discovery, with
  the old entities removed;
* a stale `Heating loop 1` demand that keeps its intent, raises `301` and
  recovers on a fresh `ON`;
* a floor demand that runs the pump and automatic heater, then stops the heater
  immediately while the pump finishes its after-run and stops.

---

## 5. How to flash and check

From `firmware/` (the attached bench board is a plain ESP32, so use
`esp32dev_sim`; `esp32s2` is compile-only for now):

```bash
pio remote run --force-remote -d . -e esp32dev_sim --target uploadfs
pio remote run --force-remote -d . -e esp32dev_sim --target upload
python3 ../tests/runtime_check.py --base-url http://192.168.2.14 --serial
```

`tests/flash_and_check.sh --serial` (from the repository root) does the build,
flash and checks in one step; set `HPN_UPLOAD_FS=1` to include the UI assets.

Always bound the serial monitor with `timeout`; never run an unbounded
`pio remote device monitor`. The suite's `--serial` check does this for you.

---

## 6. Files in this change

Runtime commits `11e8dc9` and `bd2b3e1`:

```
firmware/data/js/app.js
firmware/data/js/app.js.gz
firmware/include/hpn/state.h
firmware/src/l2_control.cpp
firmware/src/mqtt_client.cpp
firmware/src/web_portal.cpp
```

The accompanying tests/docs commit:

```
firmware/HANDOVER.md
firmware/README.md
HANDOVER.md
plans/master_plan.md
plans/mqtt_messaging_plan.md
plans/controller node/00_index_and_architecture.md
plans/controller node/01_config_and_contracts.md
plans/controller node/03_safety_layer_l1.md
plans/controller node/04_control_and_coordination_l2.md
plans/controller node/05_modes_and_testing.md
plans/controller node/06_interfaces_and_logging.md
plans/controller node/web_interface.md
tests/README.md
tests/runtime_check.py
```

---

## 7. Open items

1. **Flash and run the suite on the bench board.** The local build is green, but
   the new discovery cleanup, keep-alive and after-run paths have not run on
   hardware.
2. **Compile `esp32s2`** for this change set; the last compile-only success was
   before the state/MQTT changes.
3. **HA-side integration:** configure the three switches to repeat their `ON`
   commands faster than `demand_timeout_s` (default 600 s). A missed refresh
   raises `301` and drops the heat demand by design.
4. **Decide whether `pump_stop_after_heating_s` needs per-circuit values.** The
   firmware already keeps separate timers; the configuration still exposes one
   value applied to all three circuits.
5. **Version string:** `0.3.0-web` marks the new contract; bump it again once
   the hardware run passes if you want a distinguishable deployed build.

---

## 8. Change set 2 — runtime output driver / port expander (2026-10-27)

### 8.1 Objective

The legacy carrier and the next carrier have too few GPIOs, so both use a port
expander. The Hardware & Pins page previously asked for raw relay GPIOs even
when an expander was fitted, and the output polarity was compiled in. This
change makes the driver and polarity runtime configuration:

* select **direct GPIO**, **74HC595** or **PCF8574 / custom I²C expander**;
* configure a 7-bit PCF8574 address (standard `0x20–0x27` / `0x38–0x3F` or a
  custom `0x08–0x77` address);
* configure the expander bit map and 74HC595 pins, `/OE` pin and `/OE` polarity;
* choose **active-high or active-low** relay/mixer outputs.

The MUX select lines stay raw/active-high in both cases.

### 8.2 Safety-relevant behaviour

* The safe output byte follows `output_active_low`: `0x00` for active-high,
  `0xFF` for active-low (with the MUX select bits forced low). The PCF8574
  board idles high, so active-low keeps its relays off during boot.
* A 74HC595 `/OE` is disabled before the safe byte is shifted, then enabled.
  The legacy carrier pulls `/OE` up, so outputs stay off until L0 drives it.
* A PCF8574 I²C write failure is logged once; the driver keeps the last safe
  byte instead of guessing.
* Expander bit assignments must be unique (0–7); duplicate bits are rejected by
  the server. 74HC595 requires data/clock/latch pins; PCF8574 requires SDA/SCL.
* Direct GPIO outputs use the same polarity mapping as the expanders.

### 8.3 Configuration format

Config is now **v3**, 1364 bytes. v1 (1288-byte prefix) and v2 (1348 bytes)
blobs migrate onto current defaults and are saved immediately as v3. The new
fields are `output_expander`, `expander_address`, the 74HC595 pins and `/OE`
polarity, `output_active_low`, and eight `expander_bit_*` values.

### 8.4 Verification

* `pio run -d firmware -e esp32dev_sim` → **SUCCESS**.
* `pio run -d firmware -e esp32s2` → **SUCCESS** (compile only).
* `node --check firmware/data/js/app.js` and `gzip -9` asset regeneration → OK.
* `python3 -m py_compile tests/runtime_check.py` → OK.
* Flashed firmware + LittleFS to the bench ESP32 (`esp32dev_sim`) on
  2026-10-27. The existing v2 blob migrated to v3 on the first boot; later
  reboots log `[config] loaded saved configuration from NVS`.
* `python3 tests/runtime_check.py --serial` → **PASSED: 89 passed, 0 failed,
  0 skipped**; without `--serial` → **PASSED: 82 passed, 0 failed, 0 skipped**.
* The serial boot log showed `[l0] carrier HomeNet Heatpumps 1.0 (74HC595
  output extender)`, `[l0] 74HC595 data=12 clock=11 latch=9 oe=7 (OE
  active-low)` and no NVS/crash markers.
* The suite round-tripped a PCF8574 selection (address, I2C pins, active-low),
  observed the reboot flag and confirmed duplicate expander bits are rejected.
* A manual emulated PCF8574 boot (address `0x27`, SDA 8 / SCL 10, active-low)
  logged `[l0] PCF8574 addr=0x27 sda=8 scl=10 (safe state written)` and
  `/api/sim` reported `outputByte = 0xF9` — every relay bit high (off) with the
  MUX select bits low. The 74HC595 default was restored and the full suite
  reran green.

**Still open:** no real PCF8574 board or ESP32-S2 carrier has been available, so
the I²C backend and the active-low relay board are compile/emulation-verified
only. Bench-test polarity with a meter before connecting pumps.
