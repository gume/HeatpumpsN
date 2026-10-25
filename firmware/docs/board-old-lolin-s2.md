# Legacy board: LOLIN S2 Mini carrier

**Status:** legacy / temporary. This is the board the controller runs on until
the new PCB arrives. The new board will have different wiring; nothing in this
document should be treated as the final I/O map.

This file documents the **physical** legacy carrier so the firmware can drive
it safely today and so the mapping is preserved after the hardware changes.
Source of truth for the electrical details is the previous ESPHome
configuration in [`../../analysis/OLD-CODE/`](../../analysis/OLD-CODE/):

- `heatpumps.yaml` — pins, 74HC595 assignment, MUX select wiring, LEDs, mixer
  timing, keep-alive behaviour
- `power.yaml` — ACS712 channels, sampling and calibration
- `temperature.yaml` — 1-Wire sensors and ROM IDs

## 1. Board and MCU

| Item | Value |
| --- | --- |
| Board | WEMOS/LOLIN S2 Mini (`lolin_s2_mini`) |
| MCU | ESP32-S2, single-core Xtensa LX7, 240 MHz |
| Framework (legacy) | ESP-IDF via ESPHome |
| Cores available to FreeRTOS | 1 (`HPN_HAS_DUAL_CORE == 0`) |
| PlatformIO env | `esp32s2` (`-DHPN_BOARD_OLD_LOLIN_S2`) |

Because the ESP32-S2 is single-core, the firmware must not pin any task to
Core 1. All tasks are created with `xTaskCreate` and run on the single core.

## 2. Pin map

| Function | GPIO | Direction | Notes |
| --- | --- | --- | --- |
| 74HC595 data (SER) | 12 | out | Shift-register serial data |
| 74HC595 clock (SRCLK) | 11 | out | Shift-register clock |
| 74HC595 latch (RCLK) | 9 | out | Rising edge transfers shift register to output latch |
| 74HC595 output enable (`OE`) | 7 | out | **Active low**: `LOW` = outputs enabled, `HIGH` = outputs Hi-Z |
| Pump-current MUX analogue input | 3 | ADC | ESP32-S2 `ADC1_CH2`, 12-bit, 12 dB attenuation |
| Addressable LED data | 16 | out | 5 × WS2812, GRB order |
| 1-Wire bus | 18 | in/out | DS18B20 bus, external pull-up on the carrier |
| MUX S2 / S3 "dummy" | 5 | out | Tied low; only MUX channels 0–3 are used |

Unused in the legacy configuration: `led2_pin: GPIO15`. It is defined in the
old YAML but no second LED strip or other device uses it. The firmware does not
drive GPIO15.

> GPIO numbering is the raw ESP32-S2 GPIO number, not the `Dx` silkscreen
> labels of the S2 Mini. The comments in the old YAML (e.g. `temp_pin: GPIO18`)
> already refer to raw GPIOs.

## 3. Output expansion: 74HC595

The old carrier has no direct relay GPIOs. A single 74HC595 drives the relay
board and the analogue-MUX select lines. All logical outputs therefore share
one 8-bit shift register and one latch.

Shift order is `MSBFIRST` with **bit 0 on Q0 (QA)**, matching the legacy
ESPHome `sn74hc595` component.

| Bit | Q pin | Signal | Meaning |
| --- | --- | --- | --- |
| 0 | Q0 | `heater` | Boiler heat-request relay (legacy "Heating") |
| 1 | Q1 | MUX `S1` | Pump-current MUX select, high bit |
| 2 | Q2 | MUX `S0` | Pump-current MUX select, low bit |
| 3 | Q3 | mixer `direction` | `1` = open/increase, `0` = close/decrease |
| 4 | Q4 | mixer `motion` | `1` = motor energised |
| 5 | Q5 | `pump_rad1` | Legacy "Pump 1" |
| 6 | Q6 | `pump_rad2` | Legacy "Pump 2" |
| 7 | Q7 | `pump_floor` | Legacy "Pump 3" (currently the floor circuit) |

Naming note: the legacy labels are `Pump 1/2/3`; the firmware names them by
hydraulic role (`pump_rad1`, `pump_rad2`, `pump_floor`). Which legacy pump
serves the floor is a plumbing choice recorded in the old configuration as
`cfg_floorheating_circuit` (1, 2 or 3). The firmware currently assumes the
floor circuit is legacy `Pump 3` (Q7). **Confirm this before enabling the
floor pump.**

### 3.1 Safe output state

`0x00` on the shift register means: all pumps off, heater off, mixer motion
off (direction low), MUX channel 0 selected. That is the defined-safe state
applied at boot and on any task-startup failure.

`OE` is active low. The old ESPHome driver asserted it `HIGH` (disabled) while
configuring pins, shifted `0x00`, pulsed the latch, and only then pulled it
`LOW`. The firmware mirrors that order in `l0::begin()`.

> **Hardware caveat:** between power-on and the first firmware write, `OE` is
> floating. A pull-up resistor on `OE` to 3V3 is recommended so the outputs
> stay disabled during reset. Verify this on the carrier.

### 3.2 Timing rules for the mixer

The legacy firmware enforced a direction-change dead time on the motor:

1. motion off,
2. wait ~20 ms,
3. set direction,
4. wait ~10 ms,
5. motion on.

The planned L0 pulse state machine requires at least a 50 ms direction-change
dead time (plan 02 §2.2), which is stricter than the legacy 20 ms and
supersedes it. Any code that writes bits 3 and 4 must go through
`l0::set_mixer_motor()` so the dead time is not bypassed.

### 3.3 Shared register, single writer

The MUX select bits (1–2) live in the same byte as the relay bits. Selecting an
ADC channel therefore rewrites the whole shift register. This is safe only
because the latch holds the previous outputs while shifting, and because the
firmware keeps one authoritative copy (`g_sr_cache`) that only `T_io` mutates.
Do not write the shift register from any other task.

## 4. Pump current measurement

Four ACS712-style current sensors are multiplexed onto one ADC pin by a
74HC4051 (the legacy YAML uses the `cd74hc4067` component, but the comment and
wiring are for a 4051). `S2` and `S3` are tied to the dummy pin GPIO5, which the
firmware holds low, so only channels 0–3 are reachable.

| MUX channel (S1 S0) | Legacy signal | Firmware channel (`CurrentChannel`) |
| --- | --- | --- |
| 0 (`00`) | `acs712_2_raw` — Pump 2 | `PumpRad2` |
| 1 (`01`) | `acs712_3_raw` — Pump 3 | `PumpFloor` |
| 2 (`10`) | `acs712_m_raw` — Mixer | `Mixer` |
| 3 (`11`) | `acs712_1_raw` — Pump 1 | `PumpRad1` |

The channel numbers are stored in
`BoardConfig::current_mux_channel[]`, indexed by `CurrentChannel`.

### 4.1 Legacy sampling and scaling

Legacy ESPHome behaviour, for reference:

- `sensor: adc`, `attenuation: 12db`, `update_interval: never` (sampled on
  demand only), median filter window 7.
- Pump power: `ct_clamp`, `sample_duration: 200 ms`; mixer: `1200 ms`.
- Update schedule: every 2 s, pumps sampled 250 ms apart, mixer last.
- Calibration: `calibrate_linear` with `0.004 -> 0 W` and `0.072 -> 40 W`,
  i.e. roughly `588 W/V` above the zero point, plus a dead zone that reports
  `0 W` below `5 W`.
- The old comment notes the ACS712 is a 333 mV/A variant, and that the "real
  ADC value is multiplied by 0.19".

That calibration is empirical and tied to the legacy carrier's divider and
burden resistor. **Re-derive it with a clamp meter before trusting power
values**, and re-derive it again for the new board. The firmware keeps
`has_pump_power[i]` flags so a channel that is not populated can be disabled.

The firmware does not sample current yet; `l0::tick()` is intentionally idle.

## 5. 1-Wire temperature sensors

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

## 6. Addressable LEDs

Five WS2812 LEDs on GPIO16 in GRB order. Legacy mapping:

| LED index | Legacy id | Function | Colour semantics |
| --- | --- | --- | --- |
| 0 | `led_status` | Connectivity / system | Green = WiFi + API online; yellow pulsing = WiFi only; red pulsing = no WiFi |
| 1 | `led1` | Legacy Pump 1 (`pump_rad1`) | Off when pump off; red when the heater is on; blue when the heater is off |
| 2 | `led2` | Legacy Pump 2 (`pump_rad2`) | Same red/blue rule |
| 3 | `led3` | Legacy Pump 3 (`pump_floor`) | Same red/blue rule |
| 4 | `ledm` | Mixer | Legacy used this LED only for the status pulse effect; no pump linkage |

`Config::num_leds` is 5 for this profile. Plan 02 §1.2 defines a richer
per-subsystem colour scheme for the new board; that scheme is **not** the legacy
behaviour above and is not implemented yet.

`l0::begin()` initializes the strip and clears it; animation belongs to L4.

## 7. Legacy safety behaviour worth keeping in mind

- **Keep-alive watchdog:** every pump/heater switch-on action refreshed a
  keep-alive timestamp. Every 1 s the old firmware checked it; if any relay was
  on and `keep_alive_state + cfg_keep_alive_time_sec` (default 600 s, range
  30–1800 s) had expired, it turned all relays off in sequence. The new design
  replaces this with per-layer demand timeouts (plan 03), but the failure mode
  it protected against — a lost MQTT/HA connection leaving pumps running — is
  real and must remain covered.
- **Summer mode:** blocks all heating and pump switching and the mixer.
- **Safety override:** a manual override that auto-expires after one minute.
- **Mixer stall check:** counts stalled strokes and stops driving the mixer
  after repeated failures.
- **Sequence on pump-off:** turning off the floor pump also closed the mixer in
  the old firmware.

## 8. What the firmware does today

| Concern | Where |
| --- | --- |
| Board profile selection | `firmware/platformio.ini` (`HPN_BOARD_OLD_LOLIN_S2`) |
| Pin/bit description | `firmware/include/hpn/board.h`, `firmware/src/board.cpp` |
| Safe boot, 74HC595 init, direct-GPIO fallback | `firmware/src/l0_io.cpp` |
| Config defaults (sensor IDs, LED count, travel time) | `hpn::board_apply_defaults()` |
| MUX channel selection | `l0::select_current_channel()` |
| Task set and core placement | `firmware/src/tasks.cpp` |

Implemented: output safe state, relay control, mixer direction/motion, MUX
channel selection, LED strip initialization, RTOS task set.

Not implemented yet: 1-Wire sampling, current sampling and power plausibility,
LED status animation, mixer pulse state machine and dead reckoning, all of
L1/L2/L4. `l0::tick()` is a stub.

## 9. Bring-up checklist before connecting the plant

1. Verify `OE` polarity and add the pull-up if missing.
2. Confirm the relay board is active-high at the 74HC595 outputs.
3. Confirm which legacy pump (Q5/Q6/Q7) is the floor circuit.
4. Test outputs with lamps/relays only, never with pumps, until L1 exists.
5. Verify the 74HC595 bit map with a logic probe: set one bit at a time.
6. Verify MUX channel mapping against a known load per current sensor.
7. Re-calibrate the current sensors with a clamp meter.
8. Confirm the external over-temperature cutout and manual pump-cut switch are
   fitted; firmware is not a substitute for them.
