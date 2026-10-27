# I/O emulation

The controller is developed on a **boardless** ESP32 devkit, while the plant
currently runs a **HomeNet Heating control v0.1** board. To develop the control
layers without that hardware, L0 can run against an in-memory model of the
board instead of physical pins.

Emulation is a **compile-time** choice. There is no runtime switching and no
hardware autodetection: either the firmware talks to pins, or it talks to the
model.

## 1. Layers

```
L1/L2/L4
   │  logical calls (relay, mixer, temperature, power)
L0  firmware/src/l0_io.cpp        board logic: bits, MUX channel, safe state
   │  pin / ADC / LED / sensor calls
io  firmware/src/io_backend.cpp   compile-time switch
   ├── hardware: Arduino GPIO, shiftOut, analogRead, Adafruit_NeoPixel
   └── emulated: firmware/src/sim.cpp  in-memory model
```

The board profile (`BoardConfig`) is shared by both backends, so the emulated
v0.1 board decodes exactly the same output-extender byte, MUX channel and relay
bits as the real one. The same applies to the v1.x PCF8574 byte and its P7
fault input.

## 2. Building

| Environment | Board | Backend |
| --- | --- | --- |
| `esp32dev_sim` | HomeNet Heating control v0.1 | **emulated** — default env |
| `esp32dev_hhn1x_sim` | HomeNet Heating control v1.x | **emulated** bench |
| `esp32dev` | boardless | hardware, no pins assigned |
| `esp32s2` | HomeNet Heating control v0.1 | hardware |
| `esp32c3_hhn1x` | HomeNet Heating control v1.x | hardware |
| `esp32s3_hhn1x` | HomeNet Heating control v1.x | hardware |
| `esp32s3` / `esp32c3` | boardless | hardware, no pins assigned |

```bash
pio run -e esp32dev_sim         # default environment
pio run -e esp32dev_sim -t upload
```

The flag is `-DHPN_IO_EMULATION` (plus `-DHPN_CARRIER_HHN1` or
`-DHPN_CARRIER_HHN1X`). To emulate another board, keep the flag and change the
board macro.

At boot L0 prints which path is active:

```
[l0] board     HomeNet Heating control v0.1 (74HC595 output driver)
[l0] backend   emulation
[l0] 74HC595   data=12 clock=11 latch=9 oe=7 (OE active-low)
```

An `esp32dev_sim` image must never be expected to drive real relays. Verify the
`backend` line before connecting anything. The driver name follows the runtime
`output_expander` setting; a PCF8574 selection prints `PCF8574` and uses the
same emulated output byte.

## 3. What the model contains

`hpn::sim::snapshot()` returns a consistent copy of the whole model:

| Field | Meaning |
| --- | --- |
| `temperature_c[3]`, `temperature_valid[3]` | Effective boiler / floor-return / mix-out answers, indexed by `TempRole` |
| `power_w[4]` | Effective pump and mixer current-sense answer, indexed by `CurrentChannel` |
| `power_set_w[4]` | User-set on/moving wattage (0–40 W) |
| `relay_on[4]` | Effective emulated relay state, indexed by `Relay` |
| `relay_override[4]`, `relay_manual_on[4]` | Per-relay manual override flag and state |
| `mixer_motion`, `mixer_open` | Effective mixer bits |
| `mixer_override`, `mixer_manual_motion`, `mixer_manual_open` | Manual mixer override |
| `follow_controller` | Relays/mixer follow L0 unless manually overridden |
| `realistic_power` | Pump power follows its relay; mixer power follows motion |
| `realistic_boiler` | Boiler supply is the placeholder sine wave around 65 °C |
| `mux_channel` | Decoded MUX channel, `0xFF` if the carrier has none |
| `output_byte` | Last byte written to the selected output driver (74HC595 latch or PCF8574 port) |
| `output_enabled` | `OE` state of a 74HC595; always true for PCF8574 and direct GPIO |
| `led_count`, `led_rgb[]` | Last LED frame, logical RGB |
| `revision` | Bumped on every change; cheap change detection for the UI |

Defaults after boot: 45.0 / 30.0 / 35.0 °C, all sensors valid, pump on-power
10 W, mixer moving-power 2 W, follow-controller on, realistic current sensors
on, realistic boiler off, all outputs off, LEDs black. A reboot resets the
model.

Inputs the UI can write:

```cpp
hpn::sim::set_temperature(hpn::TempRole::Boiler, 52.5f);
hpn::sim::set_temperature(hpn::TempRole::MixOut, 41.0f);
hpn::sim::set_temperature(hpn::TempRole::ReturnFloor, 30.0f, false);  // fault
hpn::sim::set_power_w(hpn::CurrentChannel::PumpFloor, 24.0f);          // 0–40 W
hpn::sim::set_power_w(hpn::CurrentChannel::Mixer, 2.0f);
hpn::sim::set_relay_manual(hpn::Relay::Heater, true);                  // bypass L1
hpn::sim::set_mixer_manual(true, true);                                // open
hpn::sim::set_follow_controller(true);
hpn::sim::set_realistic_power(true);
hpn::sim::set_realistic_boiler(true);
hpn::sim::clear_manual_overrides();
hpn::sim::reset();
```

With `follow_controller` on, L0 drives the emulated relays and mixer. A manual
relay or mixer command overrides only that channel until the overrides are
cleared or follow mode is toggled off/on. With follow mode off, the manual
states are the only source of truth. Manual relay control deliberately bypasses
L1: it represents the carrier, not the controller's safety arbitration.

With `realistic_power` on, a pump reports its configured on-power while its
relay is on and 0 W while off. The mixer reports its configured moving-power
while motion is commanded and 0 W while idle. Turn the switch off to write the
current-sense answer directly (useful for dry-run and welded-relay tests).

`read_power_w()` is the seam L1 uses for dry-run and welded-relay
supervision: set `0 W` while the relay is on to simulate a dry run, or nonzero
power while off to simulate a welded relay. In the emulation build L0 returns
the model value directly; on hardware it converts the sampled RMS voltage with
the shared `pump_current_offset_v` / `pump_current_w_per_v` calibration from
Settings → Hardware.

All setters and `snapshot()` are safe to call from different tasks; the model
uses a short critical section. Copy sizes are small, so no mutex is needed.

## 4. What is *not* emulated

Deliberately simple — the model answers questions, it does not simulate
electronics:

- no 1-Wire bus timing, ROM search, CRC or conversion time: only the
  temperature answer and a validity flag;
- no ADC waveform, median filter or ACS712 physics: power is either set in
  watts directly or follows the relay/motion with a configured on/moving value;
- no logged boiler-temperature shape yet: the realistic mode uses a placeholder
  60–70 °C sine wave until the real form is collected;
- no relay or pump physics, no mixer mechanics, no flow or temperature
  response of the plant;
- no Wi-Fi/MQTT radio physics: the station-mode Wi-Fi client, MQTT/HA
  integration and the web portal are real. MQTT commands and telemetry use the
  same emulated carrier state as the bench page; counters stay in RAM and are
  never written to NVS under emulation.

The emulated `l0::read_current_raw()` returns `0`. Use
`l0::read_power_w(const Config&, CurrentChannel)`, which takes the shared
calibration for hardware and the model value under emulation.

## 5. Web dashboard integration

The portal serves a separate **Board emulation** page at `/emulation.html`.
It is not linked from the main menu and is only registered when the firmware is
built with `HPN_IO_EMULATION`, so it does not exist in hardware builds. The
page is a direct board control surface: it does **not** run L1 and there is no
heater/pump interlock. It uses:

- `hpn::sim::snapshot()` to render the virtual carrier;
- the `set_*` functions above when the user edits a field;
- the emulation-only REST endpoints `/api/sim`, `/api/sim/temperature`,
  `/api/sim/power`, `/api/sim/relay`, `/api/sim/mixer`, `/api/sim/mode`,
  `/api/sim/overrides/clear` and `/api/sim/reset`.

Because the HTTP server runs in the AsyncTCP task and L0 in `T_io`, the page
never blocks the control loop. The page contains:

1. Four compact relay buttons (heater, floor, radiator 1, radiator 2) that
   override the emulated board directly.
2. Mixer open/stop/close buttons.
3. Behaviour switches: follow controller, realistic current sensors and the
   placeholder realistic boiler sine wave.
4. Three temperature sliders/inputs with a validity toggle (boiler, floor
   return, mix out). The boiler input is disabled while the sine wave is on.
5. Four power inputs (floor, radiator 1, radiator 2, mixer), limited to
   0–40 W. With realistic current sensors on, the input sets the on/moving
   wattage and the page shows the effective power separately.
6. A board readout (controller output byte, MUX, OE, interlock fault, model
   revision) and a reset button.

Status LEDs are shown on the main dashboard, not on the emulation page. They
are derived from the controller's live state (system/comms, boiler, floor,
radiator 1, radiator 2) using the HomeNet Heating control v0.1 semantics in
[`../../plans/controller node/02_hardware_and_l0.md`](../../plans/controller%20node/02_hardware_and_l0.md).
The live dashboard reads the controller state through `/ws`; changes made on
the emulation page appear there after the next L0 poll.

## 6. Adding another board

When another board layout is defined:

1. Add a `BoardConfig` constant and a new `HPN_CARRIER_*` branch in
   `firmware/src/board.cpp`.
2. Add its defaults (pins, bit map, polarity, current front end, sensor IDs,
   LED count) in `board_apply_defaults()`.
3. Add envs for its MCUs with the new macro, plus an `*_sim` variant for
   emulation.
4. Update `sim.cpp` only if the board uses a shape the model does not decode
   (for example more than one shift register or a different MUX topology).
5. Document the pin map in `firmware/docs/boards.md`.
