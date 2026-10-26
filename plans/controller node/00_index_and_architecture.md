# System Plan v9 — Master Index & Architecture: Multi-Circuit Heating & Floor Mixer Controller Node (ESP32)

**Scope:** The control node (ESP32 firmware + hardware I/O) only, subordinate to the top-level **System Master Plan**. The observer (independent, external) and Home Assistant (HA) are documented here only where their controller interfaces and contracts must be defined.

**Status:** Consolidated v9 (aligned with Master Plan v1, DHW time-sharing handling where physical relay stays ON but heat is diverted, direct test-mode mixer control, abstract encoder support, and strict layer-isolated pin access).

---

## 0. LLM Context & Modular Specification Rules (Binding)

When iterating on this specification using LLMs, the following documentation rules apply across all files:

1. **Single Source of Truth for Identifiers:** All configuration parameters (`struct Config`), runtime state variables (`struct SystemState`), and reason codes are canonically defined in `01_config_and_contracts.md`. Do not invent or rename parameters in other module files without explicitly listing the required update to `01_config_and_contracts.md`.
2. **Strict Layer Isolation:** No layer except L0 (`02_hardware_and_l0.md`) may touch hardware pins or GPIOs directly. Higher layers (HA, L3, L2) only set *logical desired states/intents* (`*_intended`). L1 (`03_safety_layer_l1.md`) evaluates safety bounds and translates intents into validated physical commands (`*_phys`), which L0 executes.
3. **Scoped Prompting:** Always include `00_index_and_architecture.md` and `01_config_and_contracts.md` as read-only context when editing any specific domain module (`02` through `06`).

### Module Manifest (Table of Contents)

| File | Title & Scope | Primary Sections from Monolithic Spec |
| :--- | :--- | :--- |
| **`00_index_and_architecture.md`** | **Master Index & Software Architecture** (This file) | Overview (§1), Layer Model & FreeRTOS Tasks (§3), Open Points (§12) |
| **`01_config_and_contracts.md`** | **Configuration, Ownership & State Contracts** | Parameter Ownership & `struct Config` (§4), Shared `SystemState` (§3.3), Dual-State (`_intended` vs. `_phys`) & Reason Code Contracts |
| **`02_hardware_and_l0.md`** | **Hardware Map & L0 Actuator/Sensor Drivers** | Hardware / I/O Map (§2), L0 Drivers, 1-Wire Error Rejection, Mixer Target-Position Servo, Pump Power Sensors (§9) |
| **`03_safety_layer_l1.md`** | **Safety Layer (L1) Arbitration** | Tick-by-Tick Safety Rules, Hard Overtemp, Gradient Cutoff, Sensor Fault Postures, Lockouts & Backstops (§8) |
| **`04_control_and_coordination_l2.md`** | **Command Intake, L2 Control & Coordination** | Keep-Alive & Timeouts (§5.1), DHW Time-Sharing (§5.2), Summer Rejection (§5.3), Smith-Predictor Mixer Loop, Encoder Sync, Flush, $\Delta T$ Collapse & Coordination Table (§6) |
| **`05_modes_and_testing.md`** | **Operational Modes & Identification Tests** | Heat Mode (§7.1), Summer & Anti-Seize Exercise (§7.2), Test Override Mode, Direct Mixer Jogging & Observer ID Routines (§7.3) |
| **`06_interfaces_and_logging.md`** | **Logging, Error Codes & External Interfaces** | Slow & Event Logs, System Error Codes (§10), MQTT/HA Contract (§11.1), Web Dashboard (§11.2), Observer Interface (§11.3) |

---

## 1. System Overview

The controller node operates a hydronic heating distribution module with **three hydraulic circuits** and a **shared boiler/heater call**:

* **Floor Heating Circuit (mixed):** circulation pump (`pump_floor`), 3-point motorized mixing valve (`mixer`), floor return temperature sensor (`T_return_floor`), and mixed output temperature sensor (`T_mix_out`).
* **Radiator Circuit 1 (unmixed):** circulation pump (`pump_rad1`), commanded by HA, running at boiler supply temperature.
* **Radiator Circuit 2 (unmixed):** circulation pump (`pump_rad2`), commanded by HA, running at boiler supply temperature.
* **Shared Heater Switch (`heater`):** dry-contact relay commanding the boiler's external heat-flow switch. The boiler has **its own internal control** (modulates and diverts heating capacity to Domestic Hot Water when prioritized).

The controller performs closed-loop control of the floor mixer output, coordinates the three pumps and the shared heater switch, enforces hardware safety independently of all higher logic, logs telemetry for the observer, and exposes a local web dashboard and MQTT HA integration.

### 1.1 Division of Responsibility

* **Home Assistant (HA)** is the **single command authority**. It owns room temperature sensing, comfort logic, per-circuit after-run orchestration, floor forwarding temperature setpoints, Heat/Summer mode selection, DHW heating active notification, and pump/heater switch commands across all three circuits.
* **Controller** owns real-time floor mixer regulation, relay actuation, L1 safety arbitration, keep-alive and after-heating timeout supervision, local fallback/test mode, and telemetry logging.
* **Observer** is fully independent and intermittent: it reads logs and pushes L3-validated control loop tuning parameters (including optimizing parameters like pause durations). **It has no command path and cannot initiate tests**.
* **Boiler** owns its own burner power modulation, internal DHW time-sharing diversion, and internal over-temperature/no-flow shutdown.

### 1.2 Core Design Rules (Binding)

1. **Autonomous Safety:** Safety (L1) and actuator/sensor drivers (L0) must function with comms and adaptation absent: boot without WiFi, HA down, corrupted parameters $\rightarrow$ compiled-in defaults.
2. **Strict Layer Isolation for Pin Access:** No layer except L0 touches hardware pins or GPIOs directly. Higher layers (HA, L3, L2) only set *logical desired states/intents* (`*_intended`). L1 evaluates safety bounds and translates intents into abstract actuator/relay commands, and L0 executes the physical pin transitions.
3. **Disjoint Parameter Ownership:** Every parameter belongs to exactly one owner category (Dashboard, HA, or Observer) and is persisted in NVS within validated bounds (see `01_config_and_contracts.md`).
4. **Full Auditability:** Every action and command/physical divergence is logged as an event with full context, reason codes, and system error codes (see `06_interfaces_and_logging.md`).

---

## 2. Software Architecture

### 2.1 Layer Model & Pin Access Flow

```text
+--------------------------------------------------------+
| L4: Comms (MQTT, HA integration, Dashboard, Test Mode) |  Intake of commands (*_intended) & setpoints
+---------------------------+----------------------------+
                            | (commands)
                            v
+--------------------------------------------------------+      +-------------------------------------------+
| L2: Control Loop (Mixer loop, flush SM, coordination)  |<-----| L3: Adaptive Layer (Observer L3 params)   |
|     Calculates requested actuator states (*_requested) |      |     Validates & tunes loop coefficients   |
+---------------------------+----------------------------+      +-------------------------------------------+
                            |
                            v
+--------------------------------------------------------+
| L1: Safety Layer (Deterministic, tick-by-tick arbiter) |  Enforces safety bounds & outputs *_phys commands
+---------------------------+----------------------------+
                            |
                            v
+--------------------------------------------------------+
| L0: Actuator & Sensor Drivers (GPIO / Pin Manipulation)|  **Only layer permitted to touch physical pins**
+--------------------------------------------------------+
```

### 2.2 RTOS Task Map (FreeRTOS, Dual-Core ESP32)

**Core 0 — Protocol Core:**

| Task | Content | Priority |
| :--- | :--- | :--- |
| `T_comms` | MQTT client, HA discovery & state publishing, command ingestion, HTTP dashboard server | Medium |
| `T_logger` | Ring buffers, log upload, NVS config writes | Low |

**Core 1 — Control Core:**

| Task | Content | Priority |
| :--- | :--- | :--- |
| `T_safety` (L1) | Fast tick ($500\text{ ms}$–$1\text{ s}$), evaluates all safety rules, **owns final arbitration**, outputs validated commands to L0 | Highest |
| `T_io` (L0) | Continuous 1-wire polling, **direct GPIO pin writes**, optional pump power sensors | High |
| `T_control` (L2+L3) | Control tick (default $2\text{ s}$), mixer loop, coordination, applies L3-validated parameters | High (below L0/L1) |

*Architectural Rationale:* L1 runs in a separate highest-priority task so it keeps arbitrating even if L2 hangs; `T_io` runs with high priority to maintain precise pulse timing and non-blocking sensor acquisition; WiFi on Core 0 keeps control timing deterministic on Core 1.

### 2.3 Shared State and Messaging Overview

* **Real-Time Core Isolation:** To eliminate priority inversion hazards, `T_safety` and `T_control` on Core 1 **never block on Core 0 networking or logging tasks**. Cross-core state communication uses a lock-free snapshot mailbox or double-buffered `SystemState` (Core 1 publishes atomic state snapshots to Core 0 without waiting).
* Core 1 internal messaging uses direct command queues (`T_control -> T_safety -> T_io`) and sensor snapshot mailboxes (`T_io -> T_safety/T_control`).
* Sensor values are always published internally with monotonic timestamps and validity flags (detailed schema in `01_config_and_contracts.md`).

### 2.4 Timekeeping

* **Monotonic uptime ($\text{ms}$)** is the authoritative internal clock — all control logic, timeouts, keep-alives, and pulse timestamps use it exclusively.
* **Wall time (NTP, when available)** is attached to log records as a best-effort annotation.

### 2.5 Watchdog

The hardware watchdog is fed by `T_io` only when Core 1 real-time tasks (`T_safety`, `T_control`, and `T_io`) have all checked in within deadlines. Core 0 tasks (`T_comms`, `T_logger`) are deliberately excluded from this real-time kick path so that network drops, WiFi renegotiation, or MQTT retries never cause a controller hardware reset. Core 1 hang $\rightarrow$ reset $\rightarrow$ defined-safe boot.

### 2.6 Fail Independence Matrix

| Failure | Behavior |
| :--- | :--- |
| **WiFi / HA down** | Active heating stops after `demand_timeout_s`; running pumps stop after `pump_stop_after_heating_s`; local Test mode available for manual operation |
| **Observer down / absent** | Control continues indefinitely with last valid NVS parameters |
| **Parameter NVS corrupted** | Compiled-in defaults loaded, alarm flag raised |
| **Sensor open-circuit / glitch / stale** | Filtered at L0; persistent fault triggers L1 fail-safe posture (see `02_hardware_and_l0.md` and `03_safety_layer_l1.md`) |
| **L2 crash** | L1 continues; L0 honors L1 arbitration |

---

## 3. Open Points (Remaining Module-Level Details)

1. **Flush early-exit criterion (`04_control_and_coordination_l2.md`):** Keep pure timer (`start_flush_ms`) as default until initial Observer field logs confirm whether `flush_delta_t_ok` early-exit is beneficial.
2. **Encoder update rate & staleness window (`04_control_and_coordination_l2.md`):** Confirm reporting interval of the position sensor to set the staleness threshold before raising a warning during periodic checks.
3. **Hardware specifics for optional sensors & UI (`02_hardware_and_l0.md`):** Finalize magnetic encoder part (e.g., AS5600) for future local I2C wiring, assign TBD GPIO pins, and finalize the addressable RGB LED chain function mapping.