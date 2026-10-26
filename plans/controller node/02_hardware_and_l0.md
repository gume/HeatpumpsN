# 02 — Hardware / I/O Map & Actuator/Sensor Drivers (L0)

**Parent:** `00_index_and_architecture.md`  
**Reads/Depends on:** `01_config_and_contracts.md` (Category 1 hardware/GPIO config), `03_safety_layer_l1.md` (arbitrated `*_phys` commands)  
**Outputs to:** `03_safety_layer_l1.md` & `04_control_and_coordination_l2.md` (filtered `SensorReading` structs, encoder feedback, pump power telemetry)

---

## 1. Hardware / I/O Map

| Signal | Type | GPIO / Assignment | Notes |
| :--- | :--- | :--- | :--- |
| `pump_floor` relay | Relay, $230\text{ V}$ | `gpio_pump_floor` (TBD) | Floor circuit pump, on/off. Optional power sensor feedback (§2.5) |
| `pump_rad1` relay | Relay, $230\text{ V}$ | `gpio_pump_rad1` (TBD) | Radiator circuit 1 pump, on/off. Optional power sensor |
| `pump_rad2` relay | Relay, $230\text{ V}$ | `gpio_pump_rad2` (TBD) | Radiator circuit 2 pump, on/off. Optional power sensor |
| `heater` switch relay | Relay, **dry contacts** | `gpio_heater` (TBD) | Commands the boiler's external heat-flow switch |
| `mixer` motor open | Relay/Triac, $230\text{ V}$ | `gpio_mix_open` (TBD) | Pulsed; interlocked with close |
| `mixer` motor close | Relay/Triac, $230\text{ V}$ | `gpio_mix_close` (TBD) | Pulsed; **hardware interlock** with open |
| Mixer Encoder (Abstracted) | Generic Position Input | Abstracted (`pos_source`) | Accelerometer forwarded via HA (`POS_SRC_HA_GENERIC`) or future magnetic encoder on local $\text{I}^2\text{C}$ (`POS_SRC_I2C`, pins `i2c_sda`, `i2c_scl` TBD). |
| `T_boiler` | DS18B20, 1-wire | `gpio_onewire` (TBD) | Heating water supply temperature (ROM ID: `sensor_id_boiler`) |
| `T_return_floor` | DS18B20, 1-wire | `gpio_onewire` (TBD) | Floor circuit return temperature (ROM ID: `sensor_id_ret`) |
| `T_mix_out` | DS18B20, 1-wire | `gpio_onewire` (TBD) | Mixer output temperature — primary control + safety (ROM ID: `sensor_id_mix`) |
| Status LEDs | Addressable RGB (WS2812B/SK6812) | `gpio_led_data` (TBD) | Single data pin driving up to 5 chained RGB LEDs (§1.2) |
| Comms | WiFi | ESP32 internal | MQTT (primary), HTTP dashboard, serial debug |

**Current hardware:** the plant runs the **HomeNet Heatpumps 1.0** carrier
documented in
[`../../firmware/docs/hhn-1.0.md`](../../firmware/docs/hhn-1.0.md), with an
ESP32-S2 (LOLIN S2 Mini) fitted. On that carrier the pins above are not direct
GPIOs: relays and the analogue-MUX select lines are driven by a 74HC595, pump
current is multiplexed onto one ADC pin, and the LED chain has 5 elements. The
`TBD` assignments become concrete only with **HomeNet Heatpumps 2.0** (ESP32-C3
and ESP32-S3 primary targets); until then the `HHN1` carrier profile supplies
the mapping.

L0 is written against a compile-time I/O backend, so the same layers can run
against the real 1.0 carrier or against an emulated 1.0 carrier on a boardless
devkit (`-DHPN_IO_EMULATION`). See
[`../../firmware/docs/io-emulation.md`](../../firmware/docs/io-emulation.md).
The planned web dashboard exposes the emulated temperatures and pump power on a
dedicated page.

### 1.1 Hardware Architecture & Boot Rules

* **Physical Layer 0 Protection:** An external hardware over-temperature cutout on the floor circuit and a manual user pump-cut switch exist in hardware independent of the ESP32.
* **DS18B20 Sensors:** Identified by $64\text{-bit}$ ROM IDs in configuration (`sensor_id_boiler`, `sensor_id_ret`, `sensor_id_mix`), making them wire-order independent on a shared 1-Wire bus (`gpio_onewire`).
* **Boot-State Rule:** All outputs initialize to a defined-safe state before WiFi initialization — all three pumps `OFF`, heater switch `OFF`, mixer held until first valid sensor reading, then driven to **closed** ($0.0 = \text{mix\_safe\_pos}$).
* **Sensor Locality:** Room temperatures and radiator circuit water temperatures live in **Home Assistant**, not on this board. Only `T_boiler`, `T_return_floor`, and `T_mix_out` are measured locally.

### 1.2 Addressable RGB LED Mapping (Recommended 5-LED Chain)

Using addressable RGB LEDs (WS2812B or SK6812) requires only **one single GPIO pin** (`gpio_led_data`) while providing dedicated, glanceable status for each hydraulic element. A 5-LED layout maps 1:1 to the system components with zero ambiguity:

| LED # | Assigned Subsystem | Color & Pattern Semantics |
| :--- | :--- | :--- |
| **LED 0** | **System & Comms** | **Green:** Online, MQTT connected, normal.<br>**Yellow (pulsing):** WiFi / MQTT reconnecting.<br>**Red (flashing):** L1 Safety Trip or Critical Alarm.<br>**Blue:** Booting / Web Captive Portal active. |
| **LED 1** | **Shared Boiler (`heater`)** | **Off:** Standby (`heater_phys == OFF`).<br>**Orange:** Active heating call (`heater_phys == ON`).<br>**Cyan:** Boiler DHW Priority active (`dhw_active == true`).<br>**Purple/Blinking:** Anti-short-cycle delay holding heater off. |
| **LED 2** | **Floor Circuit (`pump_floor` + mixer)** | **Off:** Circuit idle.<br>**Green:** Pump running, mixer at setpoint.<br>**Cyan (pulsing):** Warm-up flush in progress.<br>**Magenta:** Floor $\Delta T$ collapse pause active.<br>**Red (rapid blink):** Floor over-temperature cutoff. |
| **LED 3** | **Radiator Circuit 1 (`pump_rad1`)** | **Off:** Pump idle (`pump_rad1_phys == OFF`).<br>**Orange:** Pump running (`pump_rad1_phys == ON`).<br>**Red:** Welded relay or pump dry-run fault. |
| **LED 4** | **Radiator Circuit 2 (`pump_rad2`)** | **Off:** Pump idle (`pump_rad2_phys == OFF`).<br>**Orange:** Pump running (`pump_rad2_phys == ON`).<br>**Red:** Welded relay or pump dry-run fault. |

*(Note: If fewer LEDs are preferred, LEDs 3 and 4 can be omitted, leaving 3 LEDs for System, Boiler, and Floor).*

*(L4 implements this table in `hpn::led::tick` (`firmware/src/led_status.cpp`); L0 owns the `set_leds` write. LED 0's blue boot/captive-portal state is reserved until the Wi-Fi manager provides an AP/portal mode. On HomeNet Heatpumps 1.0 the chain has 5 LEDs but the old firmware mapped them differently — LED 1–3 to the three pumps, LED 4 to the mixer — see [`hhn-1.0.md`](../../firmware/docs/hhn-1.0.md) §6. The table above is the implemented semantics for the new carrier.)*

---

## 2. Actuator & Sensor Drivers (L0 — Task `T_io`)

### 2.1 Pin Access Enforcement

* L0 (`T_io` on Core 1) is the **only layer** permitted to execute physical pin writes (`digitalWrite`, PWM/relay toggling).
* All higher layers merely set logical intents (`*_intended` or `*_requested`) that L1 validates and passes down to L0 as `*_phys` commands.

### 2.2 Mixer Actuator (Target-Position Servo) & Zero-Homing Policy

* **Command Model:** There is no pulse train, pulse length or cooldown. Every layer
  sets `mix_target_pos`; L1 is the single servo that energizes the motor and runs
  it **continuously** until the dead-reckoned position reaches the target, a soft
  end stop ($0\%$/$100\%$) or the free-run deadline. `min_pulse_ms`, `max_pulse_ms`
  and `pulse_cooldown_ms` no longer exist.
* **Direction-Change Dead-Time:** When the target requires the opposite direction
  while the motor is energized, L1 stops the motion, waits a short
  `kMixerDirSwitchMs` ($50\text{ ms}$) for the relay contacts to release, then sets
  the new direction and re-energizes. A reversal (for example target $40\% \to
  20\%$ while opening) therefore stops and restarts immediately, with no
  cooldown. Manual bench nudges step the target by $\pm 2\%$.
* **Dead Reckoning:** Integrates the energized duration over `travel_time_ms` to
  maintain `pos_est_dr` ($0.0$–$1.0$).
* **End-Stop Zero-Homing Policy:** To prevent excessive mechanical wear, full
  overdrive zero-homing is **not** performed on regular closed-loop moves. It is
  strictly limited to:
  1. Initial controller boot,
  2. Completion of Summer Anti-Seize exercise or Test 1 stroke sweep,
  3. Re-synchronization when $|\text{pos\_est\_dr} - \text{pos\_encoder}| > \text{recalc\_threshold}$, and
  4. Prolonged idle closure (e.g., continuous idle at $0.0$ for $> 24\text{ hours}$).
  Homing and sweep run in *free-run* mode (`mix_free_run_dir` + deadline), which
  ignores the target and the soft limits; routine moves travel only as far as the
  target requires.

### 2.3 Relay Hardware & Software Interlocks

* Mixer `OPEN` and `CLOSE` relays can never be energized simultaneously. This is
  enforced both by physical relay wiring interlocks and by the $50\text{ ms}$
  stop-wait-switch sequence in L1's mixer servo.
* On HomeNet Heatpumps 1.0 the mixer has **one** direction bit plus a motion bit in
  the 74HC595 instead of separate open/close relays. There is no hardware
  interlock on that carrier, so the L1 sequence (motion off $\rightarrow$ wait
  $\rightarrow$ set direction $\rightarrow$ motion on) is the only protection.

### 2.4 Steady-Rate 1-Wire Temperature Polling & Error Filtering

* **Simultaneous Broadcast Conversion:** Because 3 DS18B20 sensors are connected to the shared 1-Wire bus (`gpio_onewire`), sequential conversion at 12-bit resolution ($3 \times 750\text{ ms} = 2250\text{ ms}$) would breach the $1000\text{ ms}$ polling cycle. `T_io` initiates a **broadcast conversion** (`SKIP ROM 0xCC` $\rightarrow$ `CONVERT T 0x44`), waits $750\text{ ms}$ non-blockingly, and then sequentially reads each sensor's scratchpad using `MATCH ROM 0x55` with its configured ROM ID (~$15\text{ ms}$ each). Total active bus time is ~$\approx 800\text{ ms}$, comfortably fitting within `temp_poll_interval_ms = 1000 ms`.
* **Hardware Error Rejection:** L0 explicitly detects and rejects 1-Wire / DS18B20 hardware fault codes before passing data to L1/L2:
  * $-127.0\ ^{\circ}\text{C}$ (`DEVICE_DISCONNECTED_C` / open circuit / bus short),
  * $+85.0\ ^{\circ}\text{C}$ (DS18B20 power-on reset default register value when read after a parasitic/VDD brownout),
  * CRC mismatches, and
  * Physically impossible slew-rate jumps (e.g., $|\Delta T| > 10\ ^{\circ}\text{C}$ in $1\text{ s}$).
* **Glitch vs. Persistent Fault Escalation:** Single bad reads are discarded as transient glitches, increment `consecutive_errors`, and are logged in the event stream. Only after `sensor_error_threshold` consecutive invalid reads (e.g., 3 reads) does L0 set `is_valid = false` to trigger an L1 persistent sensor fault posture (see `03_safety_layer_l1.md`).
* **Gradient Calculation:** Temperature gradient $dT/dt$ ($\text{K/s}$) is smoothed using a rolling 3-sample finite-difference filter to suppress 12-bit quantization noise ($0.0625\ ^{\circ}\text{C}$).

### 2.5 Pump Power Sensors & Reserved $\text{I}^2\text{C}$ Interface

* **Pump Power Plausibility (Optional):** If configured (`has_pump_power[i] == true`), L0 reads and publishes `pump_power_w[i]` and checks hardware plausibility:
  * `pump_*_phys == ON` with $\text{power} \approx 0\text{ W} \rightarrow$ raises `WARN_PUMP_DRY_RUN` (dry-run, disconnected pump, or manual hardware switch open).
  * `pump_*_phys == OFF` with $\text{power} > 0\text{ W} \rightarrow$ raises `ALARM_WELDED_RELAY` alarm.
* **Temporary carrier:** on HomeNet Heatpumps 1.0 the four ACS712 outputs share one ADC pin through a 74HC4051 whose select lines are bits 1–2 of the same 74HC595 as the relays, so a channel switch rewrites the output byte. `l0::select_current_channel()` is the only permitted way to change channels. Under emulation the ADC is not modelled; `l0::read_power_w()` returns the mocked watts instead.
* **Reserved $\text{I}^2\text{C}$ Port:** Initialized on pins `i2c_sda` and `i2c_scl` when `pos_source == POS_SRC_I2C` for future direct magnetic encoder attachment (e.g., AS5600).