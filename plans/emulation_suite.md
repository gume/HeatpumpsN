# System Specification: Hardware & Plant Emulation Suite

**Parent Documents:** `plans/master_plan.md`, `plans/controller node/00_index_and_architecture.md`, `plans/controller node/02_hardware_and_l0.md`  
**Status:** Canonical Plan (codified from implementation in `firmware/src/sim.cpp`, `firmware/src/io_backend.cpp`, `firmware/data/emulation.html`, and `firmware/docs/io-emulation.md`)  
**Scope:** In-memory hardware carrier emulation, thermodynamic plant response simulation, and browser-based testbench interface.

---

## 1. Purpose & Objectives

The Emulation Suite provides an autonomous, host-decoupled test and development environment directly on the ESP32. It fulfills three critical operational requirements:

1. **Boardless Firmware Development:** Enables complete development and testing of all higher control layers (L1 Safety Arbiter, L2 Multi-Circuit Coordinator, L4 Comms & Web Portal) on a standard ESP32 devkit without physical connection to a HomeNet Heating control board, high-voltage relays, or plumbing.
2. **Deterministic Fault Injection:** Allows manual and automated simulation of critical edge-cases that are dangerous or difficult to reproduce on live equipment, including:
   * Hard floor overtemperature cutoffs ($T_{\text{mix\_out}} \ge 50.0\ ^{\circ}\text{C}$).
   * Temperature gradient spikes ($dT/dt > 0.3\ \text{K/s}$).
   * 1-Wire sensor dropouts and open circuits.
   * Pump dry-run conditions (pump relay energized with zero power draw).
   * Welded relay contacts (nonzero current sensed while pump relay is de-energized).
3. **Interactive Bench Verification:** Exposes an isolated, zero-dependency browser interface (`/emulation.html`) allowing manual adjustment of thermal inputs, power readings, relay overrides, and mixer travel.

---

## 2. Architecture & Compile-Time Backend Abstraction

Emulation operates strictly at Layer 0 (L0) and the hardware I/O backend abstraction seam (`hpn::io`). Higher control layers (L1, L2, L4) remain completely unaware of whether they are driving physical transistors or an in-memory simulation model.

```
+--------------------------------------------------------------------------+
| Layer 4 (Comms, Web Portal, MQTT) / Layer 2 (Control) / Layer 1 (Safety) |
+--------------------------------------------------------------------------+
                                    │
                                    ▼ (Logical commands: set_relay, read_temperature)
+--------------------------------------------------------------------------+
| Layer 0: Actuator & Sensor Driver (`l0_io.cpp`)                          |
| - Decodes logical relays into carrier shift-register bits or GPIOs       |
| - Translates MUX channels and schedules steady-rate polling              |
+--------------------------------------------------------------------------+
                                    │
                         Compile-Time Selection
                         (-DHPN_IO_EMULATION)
                                    ├───► [NO] ──► Physical Hardware Backend
                                    │              (GPIO, 74HC595, OneWire, ADC)
                                    │
                                    └───► [YES] ─► In-Memory Emulation Backend
                                                   (`io_backend.cpp` + `sim.cpp`)
```

### 2.1 Build Environments

The emulation backend is selected at compile-time via PlatformIO build flags:

| Environment | Carrier Profile | Backend Target | Description |
| :--- | :--- | :--- | :--- |
| `esp32dev_sim` | `HPN_CARRIER_HHN1` | `-DHPN_IO_EMULATION` | **Default bench target.** Emulates HomeNet Heating control v0.1 (74HC595 shift register, 74HC4051 current MUX, 5 LEDs). |
| `esp32dev` | `HPN_CARRIER_BOARDLESS` | Physical Hardware | Devkit target without pin assignments (stubs hardware). |
| `esp32s2` | `HPN_CARRIER_HHN1` | Physical Hardware | Production target for physical HomeNet Heating control v0.1 board. |
| `esp32c3` / `esp32s3` | `HPN_CARRIER_BOARDLESS` | Physical Hardware | Future board targets. |

> [!CAUTION]
> Firmware built with `-DHPN_IO_EMULATION` never manipulates physical GPIO pins. It must never be used to control live heating equipment. At startup, L0 explicitly announces the active backend on the serial console:
> `[l0] backend   emulation`

---

## 3. Emulation Model Specification (`hpn::sim::Model`)

The emulation state machine is encapsulated within `firmware/src/sim.cpp` and protected by FreeRTOS critical sections (`taskENTER_CRITICAL` / `taskEXIT_CRITICAL`). Tasks can query atomic snapshots via `hpn::sim::snapshot()`.

### 3.1 Data Structures & State Model

```cpp
struct Snapshot {
  // --- Thermal States ---
  float temperature_c[3];        // Boiler, Floor Return, Mixed Forward
  bool  temperature_valid[3];    // Sensor health / disconnected flags

  // --- Current & Power States ---
  float power_w[4];              // Effective wattage: Floor, Rad1, Rad2, Mixer
  float power_set_w[4];          // Configured nominal active draw (0–40 W)

  // --- Relays & Actuators ---
  bool  relay_on[4];             // Effective relay state: Heater, Rad1, Rad2, Floor
  bool  relay_override[4];       // Manual carrier override engaged
  bool  relay_manual_on[4];      // Manual forced relay state
  bool  mixer_motion;            // Mixer motor energized
  bool  mixer_open;              // Mixer driving direction (true=open, false=close)
  bool  mixer_override;          // Manual mixer override engaged
  float mixer_position;          // Physical valve position [0.0 .. 1.0]

  // --- Carrier Telemetry & Registers ---
  uint8_t mux_channel;           // Decoded 74HC4051 analog MUX channel
  uint8_t output_byte;           // Last byte of the selected output driver
  bool    output_enabled;        // Output Enable (OE) state
  uint16_t led_count;            // Number of chained addressable LEDs
  uint8_t led_rgb[15];           // Captured RGB pixel buffer (5 LEDs * 3 bytes)

  // --- Behavior Modes ---
  bool follow_controller;        // Relays reflect controller L0 outputs
  bool realistic_power;          // Wattage automatically follows relay actuation
  bool realistic_boiler;         // Boiler temperature oscillates dynamically
  uint32_t revision;             // Monotonic update counter for UI change-tracking
};
```

### 3.2 Simulation Subsystems

#### 1. Temperature Simulation
* **Boiler Supply (`TempRole::Boiler`):**
  * *Manual Mode:* Fixed value set via UI (15.0 to 85.0 °C).
  * *Realistic Mode (`realistic_boiler = true`):* Implements a smooth placeholder boiler heating cycle using a slow sinusoidal function:
    $$T_{\text{boiler}}(t) = 65.0\ ^{\circ}\text{C} + 5.0\ ^{\circ}\text{C} \cdot \sin\left(\frac{2\pi \cdot t}{120000\ \text{ms}}\right)$$
* **Floor Return (`TempRole::ReturnFloor`):**
  * Set directly via UI (default: 30.0 °C).
  * Can be toggled invalid (`valid = false`) to simulate sensor open circuit / -127 °C bus failure.
* **Mixed Forward Output (`TempRole::MixOut`):**
  * Set directly via UI (default: 35.0 °C).
  * Can be raised above `t_max_hard` (50.0 °C) to evaluate L1 overtemperature emergency latching, or pulsed rapidly to trigger gradient limit cutoffs ($dT/dt > 0.3\ \text{K/s}$).

#### 2. Carrier & Actuator Simulation
* **Output driver:** L0 writes the selected driver into the model. For a
  74HC595 the emulator captures the latch byte; for a PCF8574 it captures the
  I²C port byte. Both feed the same relay/MUX model, and
  the per-output `output_active_low_*` flags invert each relay. The default v0.1 bit
  map is:
  * Bit 0: Heater switch
  * Bit 3: Mixer direction
  * Bit 4: Mixer motion enable
  * Bit 5: Radiator pump 1
  * Bit 6: Radiator pump 2
  * Bit 7: Floor pump
* **Analog Current MUX (74HC4051):** Decodes the configured S0/S1 expander bits (default 2 & 1) to determine the active ACS712 current measurement channel. MUX bits stay active-high regardless of relay polarity:
  * Channel 0: Radiator 2 pump
  * Channel 1: Floor pump
  * Channel 2: Mixer motor
  * Channel 3: Radiator 1 pump
* **Mixer Mechanical Travel Integration:**
  * When `mixer_motion == true`, position integrates continuously:
    $$\Delta \text{pos} = \frac{\Delta t}{\text{travel\_time\_ms}} \cdot (\text{direction} == \text{OPEN} ? +1 : -1)$$
  * Clamps at physical hard limits $[0.0, 1.0]$.
* **Addressable RGB LEDs:** Captures frames sent via `io::leds_show()` and stores them in `led_rgb` buffer for visual inspection.

#### 3. Power & Current Plausibility Simulation
* **Realistic Mode (`realistic_power = true`):**
  * Active pump relay $\implies$ reports configured `power_set_w` (default 10.0 W).
  * Inactive pump relay $\implies$ reports 0.0 W.
  * Mixer motor moving $\implies$ reports configured mixer draw (default 2.0 W).
  * Mixer idle $\implies$ reports 0.0 W.
* **Fault Injection Mode (`realistic_power = false`):**
  * The user can decouple power reporting from relay states.
  * *Dry-run injection:* Relay commanded ON, power set to 0.0 W $\implies$ triggers `WARN_PUMP_DRY_RUN`.
  * *Welded relay injection:* Relay commanded OFF, power set to 15.0 W $\implies$ triggers `ALARM_WELDED_RELAY` and trips emergency lockout.

#### 4. Carrier Bypass Mode (`follow_controller = false` / `relay_override[i] = true`)
* The emulation layer can deliberately decouple from controller commands to represent hardware carrier faults or manual test-switch overrides.
* When an override is engaged, the emulated relay state is forced by the emulation surface, bypassing L1 safety arbitration.

---

## 4. Web Emulation Interface (`/emulation.html`)

Under emulation builds, the web server serves a dedicated bench dashboard at `/emulation.html` (backed by `firmware/data/js/emulation.js`).

### 4.1 UI Layout & Interaction Surface

```
┌────────────────────────────────────────────────────────────────────────┐
│  Board Emulation — HomeNet Heating control v0.1   [Revision: #1042]    │
├───────────────────────────────────┬────────────────────────────────────┤
│ CARRIER RELAYS (Bypass L1)        │ BEHAVIOR MODES                     │
│ [ Heater: OFF ] [ Floor: ON ]     │ [x] Follow Controller              │
│ [ Rad 1:  OFF ] [ Rad 2: OFF]     │ [x] Realistic Power Sensors        │
│ [ Mixer: IDLE / STOP ]            │ [ ] Realistic Boiler Sine Wave     │
├───────────────────────────────────┴────────────────────────────────────┤
│ TEMPERATURE INPUTS & FAULT INJECTION (15–85 °C)                        │
│ Boiler Supply:  [ 62.4 °C ] ──[ Slider ]──  [x] Sensor Valid           │
│ Floor Return:   [ 28.5 °C ] ──[ Slider ]──  [x] Sensor Valid           │
│ Mixed Flow:     [ 34.2 °C ] ──[ Slider ]──  [x] Sensor Valid           │
├────────────────────────────────────────────────────────────────────────┤
│ CURRENT & POWER INJECTION (0–40 W)                                     │
│ Floor Pump:     [ 10.0 W ] (Live: 10.0 W)                              │
│ Rad 1 Pump:     [ 10.0 W ] (Live:  0.0 W)                              │
│ Rad 2 Pump:     [ 10.0 W ] (Live:  0.0 W)                              │
│ Mixer Motor:    [  2.0 W ] (Live:  0.0 W)                              │
├────────────────────────────────────────────────────────────────────────┤
│ CARRIER READOUTS                                                       │
│ 74HC595 Byte: 0x90 (10010000b)  │ Current MUX Channel: 1 (Floor Pump)  │
│ Shift Register OE: Enabled      │ [ Clear All Overrides ] [ Reset Sim] │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 REST API Specification

The emulation page communicates with the controller via dedicated REST endpoints compiled only under `-DHPN_IO_EMULATION`:

| Endpoint | Method | Payload JSON | Description |
| :--- | :--- | :--- | :--- |
| `/api/sim` | `GET` | — | Returns full `Snapshot` object, current temperatures, powers, relays, and register states. |
| `/api/sim/temperature` | `POST` | `{"role":"mix", "value":55.0, "valid":true}` | Injects temperature reading or simulates sensor disconnection for `boiler`, `return`, or `mix`. |
| `/api/sim/power` | `POST` | `{"channel":"floor", "watts":15.0}` | Sets nominal power or forces arbitrary current reading on `floor`, `rad1`, `rad2`, or `mixer`. |
| `/api/sim/relay` | `POST` | `{"relay":"heater", "state":true, "override":true}` | Overrides emulated carrier relay state independently of L1 arbitration. |
| `/api/sim/mixer` | `POST` | `{"motion":true, "open":false, "override":true}` | Overrides emulated mixer motor direction and movement. |
| `/api/sim/mode` | `POST` | `{"follow":true, "power":true, "boiler":false}` | Configures simulation automation flags (`follow_controller`, `realistic_power`, `realistic_boiler`). |
| `/api/sim/overrides/clear` | `POST` | — | Clears all manual relay and mixer overrides, restoring controller tracking. |
| `/api/sim/reset` | `POST` | — | Resets the emulation model to default cold-boot parameters. |

---

## 5. Non-Persistence & Isolation Guarantee

To ensure test bench safety, the emulation engine strictly observes the following isolation invariants:

1. **Zero NVS Retention:** All simulation overrides, injected temperatures, simulated fault states, and virtual relay positions exist strictly in volatile RAM. A reboot or power cycle completely resets the simulation model.
2. **Relay Counter Protection:** Under emulation, relay actuation counters are maintained in RAM only and are **never flushed to NVS flash storage**, preventing flash wear during high-frequency testing.
3. **Log Distinction:** All manual overrides and simulation parameter changes injected through `/api/sim` generate distinct event log codes (1500–1510) in the event stream, ensuring test artifacts are easily distinguished from genuine controller actions.
