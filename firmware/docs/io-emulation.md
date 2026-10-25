# I/O emulation

The controller is developed on a **boardless** ESP32 devkit, while the plant
currently runs a **HomeNet Heatpumps 1.0** carrier. To develop the control
layers without that hardware, L0 can run against an in-memory model of the
carrier instead of physical pins.

Emulation is a **compile-time** choice. There is no runtime switching and no
hardware autodetection: either the firmware talks to pins, or it talks to the
model.

## 1. Layers

```
L1/L2/L4
   │  logical calls (relay, mixer, temperature, power)
L0  firmware/src/l0_io.cpp        carrier logic: bits, MUX channel, safe state
   │  pin / ADC / LED / sensor calls
io  firmware/src/io_backend.cpp   compile-time switch
   ├── hardware: Arduino GPIO, shiftOut, analogRead, Adafruit_NeoPixel
   └── emulated: firmware/src/sim.cpp  in-memory model
```

The carrier profile (`BoardConfig`) is shared by both backends, so the
emulated 1.0 carrier decodes exactly the same 74HC595 byte, MUX channel and
relay bits as the real one.

## 2. Building

| Environment | Carrier | Backend |
| --- | --- | --- |
| `esp32dev_sim` | HomeNet Heatpumps 1.0 | **emulated** — default env |
| `esp32dev` | boardless | hardware, no pins assigned |
| `esp32s2` | HomeNet Heatpumps 1.0 | hardware |
| `esp32s3` / `esp32c3` | boardless | hardware, 2.0 targets pending |

```bash
pio run -e esp32dev_sim         # default environment
pio run -e esp32dev_sim -t upload
```

The flag is `-DHPN_IO_EMULATION` (plus `-DHPN_CARRIER_HHN1`). To emulate a
future carrier, keep the flag and change the carrier macro.

At boot L0 prints which path is active:

```
[l0] carrier   HomeNet Heatpumps 1.0 (74HC595)
[l0] backend   emulation
```

An `esp32dev_sim` image must never be expected to drive real relays. Verify the
`backend` line before connecting anything.

## 3. What the model contains

`hpn::sim::snapshot()` returns a consistent copy of the whole model:

| Field | Meaning |
| --- | --- |
| `temperature_c[3]`, `temperature_valid[3]` | Mocked boiler / floor-return / mix-out answers, indexed by `TempRole` |
| `power_w[4]` | Mocked pump and mixer power, indexed by `CurrentChannel` |
| `relay_on[4]` | Decoded relay state, indexed by `Relay` |
| `mixer_motion`, `mixer_open` | Decoded mixer bits |
| `mux_channel` | Decoded MUX channel, `0xFF` if the carrier has none |
| `output_byte` | Last byte latched into the 74HC595 |
| `output_enabled` | `OE` state of the shift register |
| `led_count`, `led_rgb[]` | Last LED frame, logical RGB |
| `revision` | Bumped on every change; cheap change detection for the UI |

Defaults after boot: 45.0 / 30.0 / 35.0 °C, all sensors valid, 0 W everywhere,
all outputs off, LEDs black. A reboot resets the model.

Inputs the UI can write:

```cpp
hpn::sim::set_temperature(hpn::TempRole::Boiler, 52.5f);
hpn::sim::set_temperature(hpn::TempRole::MixOut, 41.0f);
hpn::sim::set_temperature(hpn::TempRole::ReturnFloor, 30.0f, false);  // fault
hpn::sim::set_power_w(hpn::CurrentChannel::PumpFloor, 42.0f);
hpn::sim::set_power_w(hpn::CurrentChannel::Mixer, 18.0f);
hpn::sim::reset();
```

`read_power_w()` is the seam L1 will use for dry-run and welded-relay
supervision: set `0 W` while the relay is on to simulate a dry run, or nonzero
power while off to simulate a welded relay.

All setters and `snapshot()` are safe to call from different tasks; the model
uses a short critical section. Copy sizes are small, so no mutex is needed.

## 4. What is *not* emulated

Deliberately simple — the model answers questions, it does not simulate
electronics:

- no 1-Wire bus timing, ROM search, CRC or conversion time: only the
  temperature answer and a validity flag;
- no ADC waveform, median filter or ACS712 physics: power is set in watts
  directly;
- no relay or pump physics, no mixer mechanics, no flow or temperature
  response of the plant;
- no Wi-Fi/MQTT radio physics: the station-mode Wi-Fi client and the web portal
  are real, MQTT is still a stub.

The emulated `l0::read_current_raw()` returns `0`. Use `l0::read_power_w()`.
On real hardware both current paths are still TODO.

## 5. Web dashboard integration

The portal serves a separate **Board emulation** page at `/emulation.html`.
It is not linked from the main menu and is only registered when the firmware is
built with `HPN_IO_EMULATION`, so it does not exist in hardware builds. The
page uses:

- `hpn::sim::snapshot()` to render the virtual carrier (temperatures, pump
  power, relay/mixer state, LED colours);
- the `set_*` functions above when the user edits a field;
- the emulation-only REST endpoints `/api/sim`, `/api/sim/temperature`,
  `/api/sim/power` and `/api/sim/reset`.

Because the HTTP server runs in the AsyncTCP task and L0 in `T_io`, the page
never blocks the control loop. The page contains:

1. Three temperature sliders/inputs with a validity toggle (boiler, floor
   return, mix out).
2. Four power inputs (floor, radiator 1, radiator 2, mixer).
3. A read-only rendering of the carrier: shift-register byte, relay states,
   mixer direction/motion, MUX channel, LED strip.
4. A reset button.

The live dashboard reads the same snapshot through `/ws`; changes made on the
emulation page appear there after the next L0 poll.

## 6. Adding HomeNet Heatpumps 2.0

When the 2.0 layout is defined:

1. Add a `BoardConfig` constant and an `HPN_CARRIER_HHN2` branch in
   `firmware/src/board.cpp`.
2. Add its defaults (pins, sensor IDs, LED count) in `board_apply_defaults()`.
3. Add envs for the primary MCUs, e.g. `esp32c3` and `esp32s3`, with
   `-DHPN_CARRIER_HHN2`, plus an `*_sim` variant for emulation.
4. Update `sim.cpp` only if the carrier uses a shape the model does not decode
   (for example more than one shift register or a different MUX topology).
5. Document the pin map in `firmware/docs/hhn-2.0.md`.
