# Board descriptions

This file is the physical description of the two controller boards the firmware
supports. It is the reference for the pin maps, the output-driver bits, output
polarity, the fault input and current sensing. The board profile is selected at
build time; the output driver and current front end are runtime configuration.

| Board | Output driver | Current sensing | MCU (current build) | PlatformIO env | Profile macro |
| --- | --- | --- | --- | --- | --- |
| HomeNet Heating control v0.1 | 74HC595 serial extender | 74HC4051 MUX + 4 × MT9221CT-06BR5 Hall sensors, one ADC pin | ESP32-S2 (LOLIN S2 Mini) | `esp32s2` | `HPN_CARRIER_HHN1` |
| HomeNet Heating control v1.x | PCF8574 I²C expander | ADS1115, optional TLV9064 sensor assembly | ESP32-S3 (v1.1 target), ESP32-C3 (bring-up) | `esp32s3_hhn1x`, `esp32c3_hhn1x` | `HPN_CARRIER_HHN1X` |
| boardless devkit | direct GPIO | none | any devkit | `esp32dev`, `esp32s3`, `esp32c3` | `HPN_CARRIER_BOARDLESS` |

The boardless profile is not a board: it is a bare devkit with every pin
unassigned, used for framework work.

The emulated bench builds use the same board profiles and the same L0 code:

| Environment | Board emulated |
| --- | --- |
| `esp32dev_sim` | HomeNet Heating control v0.1 |
| `esp32dev_hhn1x_sim` | HomeNet Heating control v1.x |

See [`io-emulation.md`](io-emulation.md) for the emulation model.

> **Schematic references.** The drawings are in `plans/schematics/`:
>
> - `SCH_HomeNet Pumper Schematic v0.1.pdf` — the board that runs the plant. It
>   has been in continuous service for over a year, so the pin map, 74HC595 bit
>   map and MUX channel map below are **confirmed by operation**.
> - `SCH_HomeNet Pumper Schematic v1.1.pdf` — the v1.x board. It is **still under
>   construction and not finalised**: protection and several pins are missing,
>   and the drawing may still change. The PCF8574/ADS1115 addresses and the
>   output bit map are confirmed by the drawn straps; the MCU pin map is not.
>
> The v1.1 drawing shows a **Wemos D1 mini footprint for mechanical
> compatibility only**. No ESP8266 build exists or is planned. The v1.1 MCU is
> intended to be an ESP32-S3 module (more pins, larger footprint), so the v1.x
> GPIO assignments in this file stay provisional until that board is finalised.

## 1. Runtime configuration model

Three settings select how the logical outputs are reached and measured. All
values are stored, and the Hardware page shows only the settings that belong to
the current selection.

| Setting | Values | Meaning |
| --- | --- | --- |
| `output_expander` | `0` direct GPIO, `1` 74HC595, `2` PCF8574 | Where relay and mixer channels are driven |
| `mixer_wiring` | `0` direction + motion, `1` open + close | Meaning of the two mixer channels |
| `current_sense` | `0` none, `1` analogue MUX + ADC, `2` ADS1115 | Current-measurement front end |

Each of the six logical outputs has its own polarity flag
(`output_active_low_*`). A board may mix active-high and active-low drivers, so
there is no global polarity. The legacy global `output_active_low` is retained
only to migrate old NVS data; it is not shown in the portal and is not used by
the output code.

The two mixer channels always mean:

| Channel | Direction + motion (`0`) | Open + close (`1`) |
| --- | --- | --- |
| 1 | direction (`1` = open) | open relay |
| 2 | motion enable | close relay |

The 74HC595 and PCF8574 both expose eight bits. The bit map for every output is
runtime configuration, so the same firmware can drive a re-fitted board. The
two expander variants do **not** share a fixed map.

## 2. HomeNet Heating control v0.1

**Status:** the board the plant runs on, in continuous service for over a year.
It accepts several MCUs in the LOLIN S2 Mini / D1-mini footprint; the unit in the
field has an ESP32-S2 (LOLIN S2 Mini). The schematic is
`plans/schematics/SCH_HomeNet Pumper Schematic v0.1.pdf` and the firmware
defaults match it.

### 2.1 MCU

| Item | Value |
| --- | --- |
| Board | HomeNet Heating control v0.1 |
| MCU fitted | WEMOS/LOLIN S2 Mini (`lolin_s2_mini`, ESP32-S2) |
| MCU | ESP32-S2, single-core Xtensa LX7, 240 MHz |
| Framework (legacy) | ESP-IDF via ESPHome |
| Cores available to FreeRTOS | 1 (`HPN_HAS_DUAL_CORE == 0`) |
| PlatformIO env | `esp32s2` (`-DHPN_CARRIER_HHN1`) |
| Board profile | `hpn::board_config()` → `HomeNet Heating control v0.1` |

Because the ESP32-S2 is single-core, the firmware must not pin any task to
Core 1. All tasks are created with `xTaskCreate` and run on the single core.

### 2.2 Pin map

| Function | GPIO | Direction | Notes |
| --- | --- | --- | --- |
| 74HC595 data (SER) | 12 | out | Shift-register serial data |
| 74HC595 clock (SRCLK) | 11 | out | Shift-register clock |
| 74HC595 latch (RCLK) | 9 | out | Rising edge transfers shift register to output latch |
| 74HC595 output enable (`OE`) | 7 | out | **Active low**: `LOW` = outputs enabled, `HIGH` = outputs Hi-Z |
| Pump-current MUX analogue input | 3 | ADC | ESP32-S2 `ADC1_CH2`, 12-bit, 12 dB attenuation |
| Addressable LED data | 16 | out | 5 × WS2812, GRB order |
| 1-Wire bus | 18 | in/out | DS18B20 bus, external pull-up on the board |
| MUX S2 / S3 "dummy" | 5 | out | Tied low; only MUX channels 0–3 are used |

Unused in the legacy configuration: `led2_pin: GPIO15`. It is defined in the
old YAML but no second LED strip or other device uses it. The firmware does not
drive GPIO15.

> GPIO numbering is the raw ESP32-S2 GPIO number, not the `Dx` silkscreen
> labels of the S2 Mini. The comments in the old YAML (e.g. `temp_pin: GPIO18`)
> already refer to raw GPIOs.

### 2.3 Output extender

The board has no direct relay GPIOs. One serial output extender drives the
relay board and the analogue-MUX select lines. All logical outputs therefore
share one 8-bit shift register and one latch.

The chain is `74HC595D` (U18) → `ULN2003ADR` (U7) → five `SRD-05VDC-SL-C`
relays. The 595 outputs are low after reset, the ULN2003 inverts them, and the
relay drivers are active-high, so a pulled-down (GND) output means the relay is
**off**. That is why the `0x00` safe byte is correct for this board with every
`output_active_low_*` flag false. Do not change the on/off levels based on a 595
datasheet alone; the polarity depends on the 595, the ULN2003 and the relay
coil driver together.

Shift order is `MSBFIRST` with **bit 0 on Q0 (QA)**, matching the legacy
ESPHome `sn74hc595` component. These are the v0.1 defaults, confirmed against
U18 in the v0.1 drawing:

| Bit | Q pin | Signal | Meaning | Active low by default |
| --- | --- | --- | --- | --- |
| 0 | Q0 | `heater` | Boiler heat-request relay | no |
| 1 | Q1 | MUX `S1` | Current MUX select, high bit | no (always active high) |
| 2 | Q2 | MUX `S0` | Current MUX select, low bit | no (always active high) |
| 3 | Q3 | mixer channel 1 | direction: `1` = open/increase | no |
| 4 | Q4 | mixer channel 2 | motion: `1` = motor energised | no |
| 5 | Q5 | `pump_rad1` | Legacy "Pump 1" | no |
| 6 | Q6 | `pump_rad2` | Legacy "Pump 2" | no |
| 7 | Q7 | `pump_floor` | Legacy "Pump 3" (floor circuit) | no |

Naming note: the legacy labels are `Pump 1/2/3`; the firmware names them by
hydraulic role (`pump_rad1`, `pump_rad2`, `pump_floor`). Which legacy pump
serves the floor is a plumbing choice recorded in the old configuration as
`cfg_floorheating_circuit` (1, 2 or 3). The firmware currently assumes the
floor circuit is legacy `Pump 3` (Q7). **Confirm this before enabling the
floor pump.**

### 2.4 Safe state and mixer timing

`0x00` on the output extender means: all pumps off, heater off, mixer motion
off (direction low), MUX channel 0 selected. Because the extender outputs pull
to GND at reset and the relays are active-high, this is also the state the
hardware itself presents before the first firmware write. It is applied again
explicitly at boot and on any task-startup failure.

`OE` is active low. The old ESPHome driver asserted it `HIGH` (disabled) while
configuring pins, shifted `0x00`, pulsed the latch, and only then pulled it
`LOW`. The firmware mirrors that order in `l0::begin()`, and derives the safe
byte from the per-output polarity flags, so the same sequence protects either
relay board.

> **Hardware caveat:** between power-on and the first firmware write, `OE` is
> floating. A pull-up resistor on `OE` to 3V3 is recommended so the outputs
> stay disabled during reset. Verify this on the board.

The legacy firmware enforced a direction-change dead time on the motor:

1. motion off,
2. wait ~20 ms,
3. set direction,
4. wait ~10 ms,
5. motion on.

The L1 layer requires at least a 50 ms direction-change dead time (plan 02
§2.2), which is stricter than the legacy 20 ms and supersedes it. Any code that
writes the two mixer channels must go through `l0::set_mixer_motor()` so the
dead time is not bypassed.

The MUX select bits (1–2) live in the same byte as the relay bits. Selecting an
ADC channel therefore rewrites the whole output byte. This is safe only because
the latch holds the previous outputs while shifting, and because the firmware
keeps one authoritative copy (`g_output_byte`) that only `T_io` mutates. Do not
write the output driver from any other task.

### 2.5 Current measurement (74HC4051 + ADC)

Four **MT9221CT-06BR5** Hall-effect sensors (NOVOSENSE, ±6 A, 333 mV/A, 5 V
supply, 2.5 V at zero current) are multiplexed onto one ADC pin by a
`74HC4051D` (U14). The MUX runs from 5 V, so its output is divided by R8/R9
(1 k series, 2 k to GND, ×0.667) before it reaches the ESP32 ADC. That gives
about **222 mV/A** at the ADC pin. `S2` is tied low; the firmware drives `S1`/`S0`
from Q1/Q2 of the shift register, so only channels 0–3 are used.

| MUX channel (S1 S0) | Sensor | Firmware channel (`CurrentChannel`) |
| --- | --- | --- |
| 0 (`00`) | `VP2` — Pump 2 | `PumpRad2` |
| 1 (`01`) | `VP3` — Pump 3 | `PumpFloor` |
| 2 (`10`) | `VM1` — Mixer motor | `Mixer` |
| 3 (`11`) | `VP1` — Pump 1 | `PumpRad1` |

This map is confirmed against the v0.1 drawing and matches the firmware.
The channel numbers are stored in `BoardConfig::current_mux_channel[]`, indexed
by `CurrentChannel`. This map is fixed by the board; the select bits are runtime
configuration (`expander_bit_mux_s0`, `expander_bit_mux_s1`).

#### Sampling and scaling

Legacy ESPHome behaviour, for reference:

- `sensor: adc`, `attenuation: 12db`, `update_interval: never` (sampled on
  demand only), median filter window 7.
- Pump power: `ct_clamp`, `sample_duration: 200 ms`; mixer: `1200 ms`.
- Update schedule: every 2 s, pumps sampled 250 ms apart, mixer last.
- Calibration: `calibrate_linear` with `0.004 -> 0 W` and `0.072 -> 40 W`,
  i.e. roughly `588 W/V` above the zero point, plus a dead zone that reports
  `0 W` below `5 W`.
- The old comment describes the sensor as a 333 mV/A variant and notes that the
  "real ADC value is multiplied by 0.19".

That `588 W/V` is an empirical ESPHome fit from the previous CT-clamp wiring.
For the four MT9221 sensors on the drawing it is not the right slope: 333 mV/A
through the 1 k/2 k divider is ~222 mV/A, and a resistive 230 V load of 1 A is
roughly 230 W, i.e. about **1.0 kW/V**. Treat the default as a placeholder.

The firmware samples current and computes power in
`l0::read_power_w(const Config&, CurrentChannel)`:

```
watts = (rms_volts - pump_current_offset_v) * pump_current_w_per_v
```

Both parameters are editable under **Settings → Hardware → Current measurement**
and are shared by every channel. The defaults reproduce the legacy fit (offset
`0.004 V`, slope `588.2 W/V`), so **re-derive them with a clamp meter before
trusting the values**, and re-derive again for v1.x. The firmware keeps
`has_pump_power[i]` flags so a channel that is not populated can be disabled.

Current-based dry-run and welded-relay protection can be switched off with
`pump_power_check_enabled` while the sensor is uncalibrated. Measurements remain
visible in the UI and MQTT; only the protection logic is skipped. Turn it back
on once the calibration is verified.

#### Known v0.1 limitations

- The 74HC4051 runs at 5 V while the shift register drives its select inputs at
  3.3 V. That is below the 4051's specified `VIH` (≈3.5 V at 5 V supply) and
  works only because the actual threshold is lower. Any respin should power the
  MUX from 3.3 V or level-shift the two select lines.
- There is **no mains protection** on the board: no fuse, no varistor/MOV, no
  X2 capacitor, no PE terminal. This is accepted for the installed unit but must
  not be repeated on v1.1.

### 2.6 1-Wire temperature sensors

One bus on GPIO18 with three DS18B20 sensors, identified by ROM ID. The legacy
configuration used 10 s update intervals and published on a 0.1 °C delta or a
60 s throttle.

| Role | `Config` field | ROM ID (legacy) |
| --- | --- | --- |
| Boiler / incoming (`T_boiler`) | `sensor_id_boiler` | `c13cdaf648ed0528` |
| Floor / mixer return (`T_return_floor`) | `sensor_id_ret` | `92c08b7f1f64ff28` |
| Mixer output (`T_mix_out`) | `sensor_id_mix` | `593c26f648ddbf28` |

The legacy YAML declares the IDs with a `0x` prefix. `Config` stores the 16 hex
digits **without** `0x` so they fit in `char[17]`; `board_apply_defaults()`
performs that conversion.

The planned L0 polling is a broadcast convert then per-ROM read (plan 02 §2.4),
not the legacy per-sensor 10 s schedule.

### 2.7 Addressable LEDs

Five WS2812 LEDs on GPIO16 in GRB order. Legacy mapping:

| LED index | Legacy id | Function | Colour semantics |
| --- | --- | --- | --- |
| 0 | `led_status` | Connectivity / system | Green = WiFi + API online; yellow pulsing = WiFi only; red pulsing = no WiFi |
| 1 | `led1` | Legacy Pump 1 (`pump_rad1`) | Off when pump off; red when the heater is on; blue when the heater is off |
| 2 | `led2` | Legacy Pump 2 (`pump_rad2`) | Same red/blue rule |
| 3 | `led3` | Legacy Pump 3 (`pump_floor`) | Same red/blue rule |
| 4 | `ledm` | Mixer | Legacy used this LED only for the status pulse effect; no pump linkage |

`Config::num_leds` is 5 for this profile. The current firmware uses the plan 02
§1.2 semantics, **not** the legacy mapping above: LED 0 is system/comms, LED 1
boiler/heater, LED 2 floor/mixer, LED 3 radiator 1 and LED 4 radiator 2. The
animator lives in `hpn::led::tick` (`firmware/src/led_status.cpp`); L0 only
writes the frame with `set_leds`. If the board's physical LED order matters,
verify it on the strip before relying on the mapping.

### 2.8 Legacy safety behaviour worth keeping in mind

- **Keep-alive watchdog:** every pump/heater switch-on action refreshed a
  keep-alive timestamp. Every 1 s the old firmware checked it; if any relay was
  on and `keep_alive_state + cfg_keep_alive_time_sec` (default 600 s, range
  30–1800 s) had expired, it turned all relays off in sequence. The new design
  replaces this with per-layer demand timeouts (plan 03), but the failure mode
  it protected against — a lost MQTT/HA connection leaving pumps running — is
  real and must remain covered.
- **Summer mode:** blocks all heating and pump switching and the mixer.
- **Safety override:** a manual override that auto-expires after one minute.
- **Mixer stall check:** the legacy firmware counted stalled strokes and
  stopped driving the mixer after repeated failures. The new firmware does not
  repeat that: the old detector relied on the supply temperature and was
  unreliable. It instead raises warning-only indications for a move longer
  than a full stroke and for a valid external encoder that does not respond
  during a commanded move.
- **Sequence on pump-off:** turning off the floor pump also closed the mixer in
  the old firmware.

## 3. HomeNet Heating control v1.x

**Status:** the next board, currently **under construction and not finalised**
(`plans/schematics/SCH_HomeNet Pumper Schematic v1.1.pdf`). The firmware profile
builds and is fully emulated, but no physical v1.1 board exists yet. The drawing
still misses mains protection and several pin assignments, and it may change
before fabrication. Do not flash a real v1.1 board without working through the
open items in §3.6.

### 3.1 MCU and assumptions

| Item | Value |
| --- | --- |
| Board | HomeNet Heating control v1.x |
| MCU | ESP32-S3 for v1.1; ESP32-C3 for bring-up on a bare devkit |
| PlatformIO env | `esp32s3_hhn1x` (v1.1, `-DHPN_CARRIER_HHN1X`), `esp32c3_hhn1x` (bring-up, same profile) |
| Board profile | `hpn::board_config()` → `HomeNet Heating control v1.x` |
| Output driver default | PCF8574, address `0x20` |
| Relay polarity default | every output active low |
| Current sensing default | ADS1115 at `0x48`, TLV9064 assembly declared absent |
| Fault input default | P7, active high (`FAULT` low = healthy) |

The v1.1 drawing shows a **Wemos D1 mini footprint** so the board can reuse the
mechanical outline. That is compatibility only: there is no ESP8266 build, and
none is planned. The carrier nets therefore keep the D1-mini names, and the
firmware maps them to the ESP32-S3 pins below.

| Carrier net | Function | ESP32-S3 GPIO | Firmware field |
| --- | --- | --- | --- |
| `D1` | I²C SCL (PCF8574 + ADS1115) | **9** | `i2c_scl` |
| `D2` | I²C SDA (PCF8574 + ADS1115) | **8** | `i2c_sda` |
| `D3` | 1-Wire `TEMP`, DS18B20 at U6 (4k7 pull-up R1) | **4** | `gpio_onewire` |
| `D4` | LED data, five TX1812 through R10 330 Ω | **5** | `gpio_led_data` |
| `TX` / `RX` | J1 programming header | 43 / 44 | UART0 |
| `A0` | dangling net label, no component | — | — |
| `D0`, `D5`–`D8`, `RST` | not connected on the drawing | — | — |

`num_leds = 5` on this profile. The LED chain is the same five status LEDs as on
v0.1 (system, boiler, floor, radiator 1, radiator 2). No analogue input is used:
current sensing is on the ADS1115, so the unconnected `A0` is irrelevant.

The chosen GPIOs avoid the ESP32-S3 strapping pins (0, 3, 45, 46), the native
USB pins (19, 20), UART0 (43, 44) and the in-package flash/PSRAM range (26–37).
The ESP32-C3 bring-up build keeps the same I²C pins but leaves 1-Wire and the
LED chain unassigned (`HPN_GPIO_UNASSIGNED`, `num_leds = 0`), because a bare
devkit has neither.

These numbers are a **firmware default, not a verified board fact**. The
schematic names the carrier nets, not the S3 pad each net lands on, and no
physical v1.x board exists yet.

Confirmed against the drawing:

- PCF8574 base address `0x20` (A0–A2 strapped to GND, `#INT` and P6 open).
- ADS1115 address `0x48` (ADDR strapped to GND).
- All six relay-driver inputs are active low (P0–P5 through series resistors
  to the SSR/relay LED cathodes).
- P7 is the fault output of the U13 NOR network, active high.
- ADS1115 inputs `AIN0=VP1, AIN1=VP2, AIN2=VM, AIN3=VP3`.
- `D1` drives `SCL` (R4 4k7 pull-up), `D2` drives `SDA` (R5 4k7), `D3` feeds the
  DS18B20 jack, `D4` feeds the LED chain, and `TX`/`RX` go only to J1.

Still unconfirmed:

- The ESP32-S3 pad each carrier net lands on. The GPIO numbers above are the
  one thing to check on the first physical module; if a net moves, change the
  default in `firmware/src/board.cpp` (or the value on the Hardware page).

### 3.2 PCF8574 output bit map

| Bit | Signal | Meaning | Direction |
| --- | --- | --- | --- |
| P0 | `pump_rad1` | Pump 1 | output |
| P1 | `pump_rad2` | Pump 2 | output |
| P2 | `pump_floor` | Pump 3 / floor | output |
| P3 | mixer channel 1 | Open relay (`mixer_wiring = open + close`) | output |
| P4 | mixer channel 2 | Close relay | output |
| P5 | `heater` | Heat request | output |
| P6 | — | unused, driven high | output |
| P7 | fault input | Interlock fault from the relay board | **input** |

The PCF8574 is quasi-bidirectional. A pin used as an input must be written high
before it is read, otherwise the device holds it low. `l0::write_outputs()`
therefore always forces P7 high before any port read, and the safe byte keeps
P7 released.

Unused outputs idle high. With active-low relay inputs, high is also the safe
OFF level, so the boot byte is `0xFF` (P7 released, every relay off). A mixed
polarity setup builds the byte per channel from each `output_active_low_*`
flag; the MUX select lines are not used on this board, and the bit fields are
stored as `255` ("not connected").

### 3.3 Interlock fault input

P7 is an **input to the MCU**, driven by the relay board's software interlock.
`FAULT = NOR(#M+, #M-)` (U13), so the line is **active high**: it is low while
the interlock is healthy and goes high when the board sees both mixer channels
requested at the same time. The same gate array then forces `MP+` and `MP-` low,
so the board drops both mixer relays in hardware before the MCU reacts. This is
a hardware-level failure: L0 reports it as
`SystemState::mix_interlock_fault`, and L1:

- raises `ALARM_MIX_INTERLOCK` and sets `ERR_MIX_INTERLOCK_FAULT` (402);
- de-energises both mixer channels;
- refuses all mixer motion while the input stays asserted.

The normal interlock (never energising open and close at the same time) is still
enforced in firmware. P7 is the independent backstop, not a replacement.

### 3.4 Current measurement (ADS1115 + optional TLV9064)

There is **no analogue MUX** on this board. Currents are read from an ADS1115
over the same I²C bus:

| ADS1115 input | Current channel (`CurrentChannel`) |
| --- | --- |
| A0 | `PumpRad1` (pump 1) |
| A1 | `PumpRad2` (pump 2) |
| A2 | `Mixer` (mixer motor) |
| A3 | `PumpFloor` (pump 3 / floor) |

The mixer has **one** current sensor: a single ring carries the open and the
close motor wire together. Because the two relays are interlocked and never
energised at the same time, that one channel always measures whichever
direction is running. All four ADS1115 inputs are therefore in use.

The mapping is stored in `BoardConfig::current_ads_channel[]` and is indexed by
`CurrentChannel` (`floor, rad1, rad2, mixer`). If the final board routes the
sensors differently, change that array in `firmware/src/board.cpp`; an entry
above `3` is not a valid ADS1115 input and reads 0 W.

The TLV9064 current-sensor assembly is an **optional external component**. Its
presence is declared with `tlv9064_present`; when it is false the firmware keeps
the ADS1115 settings but returns 0 W for every channel and current-based
protection stays off. `current_sense_available()` encodes this rule, and
`l0::power_measurement_available()` is what L1 consults before acting on a
reading.

The ADS1115 is driven directly over `Wire` (no extra library): single-shot
conversions, single-ended inputs against GND, ±4.096 V range, 860 SPS. The
firmware samples two mains cycles and reuses the shared current calibration
(`pump_current_offset_v`, `pump_current_w_per_v`).

### 3.5 Safe state and boot order

1. `l0::begin()` seeds the backend and the board profile.
2. `init_pcf8574()` writes `0xFF` (all relays off, P6 high, P7 released) before
   anything else touches the port.
3. `init_current_sense()` starts the ADS1115 and logs whether the TLV9064 is
   declared.
4. `l0::tick()` polls P7 after every output update and publishes the fault
   state to L1.

If the PCF8574 does not acknowledge, the write error is logged once and the
outputs stay in the last known safe state. If the ADS1115 does not
acknowledge, current readings stay 0 W.

### 3.6 Open items on the v1.1 drawing

These are hardware/drawing issues found while auditing the v1.1 PDF against the
firmware. None of them can be fixed in software, so they are recorded here for
the next revision.

1. **Pump SSR control nets do not connect.** The PCF8574 outputs are labelled
   `#PD1`, `#PD2`, `#PD3`; the pump SSR inputs are labelled `PD1`, `PD2`, `PD3`.
   There is no gate between them, so in the netlist the three pump SSRs have a
   floating control input. Either rename one side or draw the intended buffer.
2. **The TLV9064 front end has no bias reference.** `Vref` appears exactly once
   on the sheet (U8.1 pin 3) and nothing generates it; U8.2–U8.4 `+` inputs
   float. A single-supply transimpedance stage needs a mid-rail reference
   (≈1.65 V) on every channel before the ADS1115 readings mean anything.
   Enable `tlv9064_present` only after this is fixed.
3. **Per-channel current gain is not uniform.** U8.1–U8.3 use 15 k feedback
   (R14, R3, R9), U8.4 uses 47 k (R6), i.e. ≈3.1× more gain on `VP3`
   (pump 3 / floor). The firmware currently has one shared
   `pump_current_w_per_v`; either make the resistors equal or add a per-channel
   factor.
4. **No mains protection.** No fuse, varistor/MOV, X2 capacitor or PE terminal is
   drawn. Add them before fabrication.
5. **`APY274AEH` could not be matched to a datasheet.** Confirm that it is a
   mains-rated SSR/opto-triac suitable for inductive pump and motor loads, and
   that the 300 Ω LED resistor gives enough drive at 3.3 V.
6. **Stale sheet note.** The header note ("8574 sets output high initially,
   which switches on the relay") contradicts the drawn active-low drivers: with
   active-low inputs a high level is OFF, so the `0xFF` boot byte is the safe
   state. Delete the note so nobody "fixes" the polarity later.
7. **Pump-power supervision is armed without a sensor.** The profile sets
   `has_pump_power[] = true` and `pump_power_check_enabled` defaults to true
   while `tlv9064_present` is false. On real hardware every running pump would
   be reported as dry after 20 ticks. Either tie `has_pump_power[]` to
   `current_sense_available()` or disable the check until the front end works.
8. **The MCU pad map is not on the drawing.** U2 carries the D1-mini net names
   only (`D1` = SCL, `D2` = SDA, `D3` = TEMP, `D4` = LED); no ESP32-S3 pad is
   assigned. The firmware defaults in §3.1 (GPIO 9/8/4/5) are therefore an
   assumption. Pin them down before fabrication and re-check them on the first
   physical module.

## 4. Driver selection and reboot rules

The output driver, the expander wiring and bit map, the per-output polarity,
the fault input and the current front end are latched at startup. Changing any
of them from the portal returns `reboot_required: true`; restart the controller
before relying on the new wiring.

Selecting a driver in the portal does not rewrite the stored values of the
other driver. The Hardware page hides the unrelated settings but keeps them in
`Config`, so switching back restores the previous map.

## 5. Bring-up checklist

### HomeNet Heating control v0.1

1. Verify `OE` polarity and add the pull-up if missing.
2. Confirm the relay board is still active-high at the output-extender pins.
3. Confirm which legacy pump (Q5/Q6/Q7) is the floor circuit.
4. Verify the output-extender bit map with a logic probe: set one bit at a time.
5. Verify MUX channel mapping against a known load per current sensor.
6. Calibrate the current channels with a clamp meter.
7. Test outputs with lamps/relays only, never with pumps, until L1 has been
   verified on the emulated board.
8. Confirm the external over-temperature cutout and manual pump-cut switch are
   fitted; firmware is not a substitute for them.

### HomeNet Heating control v1.x

1. Close the open items in §3.6 first; the drawing is not final.
2. Confirm SDA/SCL against the final ESP32-S3 pin map, then the PCF8574 address.
3. Confirm the relay inputs are active low, then verify one output at a time
   with the PCF8574 (start with pumps, end with the mixer).
4. Verify P6 stays high and P7 reads low (not asserted) with the interlock
   healthy.
5. Force an interlock fault on the relay board and confirm P7 goes high, the
   mixer stops and the portal shows error 402.
6. Declare `tlv9064_present` only after the assembly is fitted and the bias
   reference exists; verify the A0/A1/A2/A3 mapping against a known load before
   enabling current protection. Run the mixer in both directions and confirm A2
   reads the moving motor.
7. Calibrate the current channels with a clamp meter.
8. Repeat the v0.1 steps 7–8.

## 6. Firmware map

| Concern | Where |
| --- | --- |
| Board profile selection | `firmware/platformio.ini` (`HPN_CARRIER_HHN1`, `HPN_CARRIER_HHN1X`) |
| Pin/bit description | `firmware/include/hpn/board.h`, `firmware/src/board.cpp` |
| Safe boot, driver init, direct-GPIO fallback | `firmware/src/l0_io.cpp` |
| Real pins vs emulation | `firmware/src/io_backend.cpp`, `firmware/src/sim.cpp` |
| Config defaults (sensor IDs, LED count, travel time) | `hpn::board_apply_defaults()` |
| MUX channel selection | `l0::select_current_channel()` |
| ADS1115 driver | `io::ads1115_*` in `firmware/src/io_backend.cpp` |
| Fault input and L1 reaction | `l0::update_interlock_fault()`, `firmware/src/l1_safety.cpp` |
| Task set and core placement | `firmware/src/tasks.cpp` |

Implemented: output safe state, relay control, mixer direction/motion, MUX
channel selection, PCF8574 output byte and fault input, ADS1115 reads, LED chain
output and the five-LED status animation, RTOS task set, emulation of both
boards, 1-Wire sampling, current sampling with a shared calibration, pump
dry-run/welded protection with a disable switch, the L1 opening
ceiling/minimum/dynamic maximum, warning-only mixer motion indications,
sensor-role plausibility supervision and the optional inferred-DHW fallback.

Partially implemented: the remaining L1/L2/L4 coordination and observer-owned
control tuning, and the physical validation of the v1.x board.
