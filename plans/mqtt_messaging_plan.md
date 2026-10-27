# System Specification: External MQTT Messaging Plan

**Parent Documents:** `plans/master_plan.md`, `plans/controller node/01_config_and_contracts.md`, `plans/controller node/06_interfaces_and_logging.md`  
**Status:** Canonical Plan (codified from current implementation in `firmware/src/mqtt_client.cpp` and `firmware/include/hpn/config.h`)  
**Scope:** Identity, Home Assistant MQTT discovery contract, inbound command handling, outbound state publishing, and dynamic external sensor discovery.

---

## 1. Overview & Transport Parameters

The MQTT interface serves as the primary external control and observation gateway between the **ESP32 Controller Node**, the central **Home Assistant (HA) hub**, and optional external sensors or observers.

### 1.1 Broker Connection & Transport Constraints

* **Transport Client:** `PubSubClient` wrapped over Arduino `WiFiClient`.
* **Buffer Allocation:** Static payload buffer of **4096 bytes** (`kMqttBufferSize`) to accommodate large Home Assistant discovery JSON payloads without heap fragmentation.
* **Connection Monitoring:**
  * Reconnection retry interval: **5000 ms** (`kReconnectIntervalMs`).
  * State publishing interval: **1000 ms** cadence (`kPublishIntervalMs`), filtered by publish-on-change caching (`StateCache`).
  * Floating-point noise deadband: $\Delta \ge 0.05$ (`kFloatPublishEpsilon`) required before re-publishing analog measurements.
* **Error Throttling:** Connection failures throttled in serial/event logs to at most once every **30 s** (`kErrorLogIntervalMs`).

### 1.2 Node Identity & Topic Prefix Scheme

At boot, the controller computes unique hardware identifiers derived from the Wi-Fi interface MAC address:

* **MAC Hex String (`g_mac_hex`):** 12-character lowercase hex string without colons (e.g., `aabbcc112233`).
* **Client ID:** `hpn-<mac>` (e.g., `hpn-aabbcc112233`).
* **Home Assistant Node ID:** `hpn_<mac>` (e.g., `hpn_aabbcc112233`).
* **Device Identifier:** `heatpumpsn_<mac>` (e.g., `heatpumpsn_aabbcc112233`).
* **Base Topic (`<base>`):** Configured via `config.mqtt_base_topic` (Default: `heatpumpsn/controller`).

### 1.3 Availability & LWT Contract

The controller establishes visibility in Home Assistant using MQTT Last Will and Testament (LWT):

| Attribute | Value |
| :--- | :--- |
| **Topic** | `<base>/status` |
| **QoS** | `1` |
| **Retained** | `true` |
| **Online Payload** | `online` (published immediately upon connection handshake) |
| **Offline Payload** | `offline` (configured as LWT payload; dispatched by broker on connection loss) |

> [!IMPORTANT]
> The `<base>/status` topic is strictly for visibility and UI state in Home Assistant. Receiving an offline LWT event must **never** trigger autonomous relay control actions on the hub; local controller timeouts (`demand_timeout_s`, `pump_stop_after_heating_s`) govern offline safety internally.

---

## 2. Home Assistant Auto-Discovery Contract

Upon successful broker connection, the controller publishes retained MQTT discovery payloads under the `homeassistant/` prefix. Home Assistant automatically constructs entities attached to a unified parent device.

### 2.1 Device Registry Block

Every discovery payload injects a shared device block:

```json
{
  "device": {
    "identifiers": ["heatpumpsn_aabbcc112233"],
    "name": "HeatpumpsN Controller",
    "manufacturer": "HeatpumpsN",
    "model": "HomeNet Heatpumps 1.0",
    "sw_version": "0.3.0-web",
    "configuration_url": "http://192.168.2.14/"
  },
  "availability_topic": "heatpumpsn/controller/status",
  "payload_available": "online",
  "payload_not_available": "offline"
}
```

### 2.2 Discovered Entities Registry

Discovery topics follow the format: `homeassistant/<component>/<node_id>/<object_id>/config`

| Component | Object ID | Entity Name | State Topic | Command Topic | Details & Units |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `switch` | `heating_loop_1` | Heating loop 1 | `<base>/state/heating_loop_1` | `<base>/cmd/heating_loop_1` | Radiator circuit 1 heat demand (`ON`/`OFF`). Reflects the effective demand after the keep-alive check. |
| `switch` | `heating_loop_2` | Heating loop 2 | `<base>/state/heating_loop_2` | `<base>/cmd/heating_loop_2` | Radiator circuit 2 heat demand (`ON`/`OFF`). Reflects the effective demand after the keep-alive check. |
| `switch` | `floor_heating` | Floor heating | `<base>/state/floor_heating` | `<base>/cmd/floor_heating` | Floor circuit heat demand (`ON`/`OFF`). Reflects the effective demand after the keep-alive check. |
| `binary_sensor` | `pump_1` | Pump 1 | `<base>/state/pump_1_physical` | — | `device_class: running`. True physical radiator-1 pump relay state (`pump_rad1_phys`). |
| `binary_sensor` | `pump_2` | Pump 2 | `<base>/state/pump_2_physical` | — | `device_class: running`. True physical radiator-2 pump relay state (`pump_rad2_phys`). |
| `binary_sensor` | `floor_pump` | Floor pump | `<base>/state/floor_pump_physical` | — | `device_class: running`. True physical floor pump relay state (`pump_floor_phys`). |
| `binary_sensor` | `heater` | Heater | `<base>/state/heater_physical` | — | `device_class: heat`. True physical heater relay state (`heater_phys`). The heater request is derived by the controller; there is no heater switch. |
| `sensor` | `power_floor` | Floor pump power | `<base>/state/power_floor` | — | `device_class: power`, unit: `W`, precision: 1. |
| `sensor` | `power_rad1` | Radiator 1 pump power | `<base>/state/power_rad1` | — | `device_class: power`, unit: `W`, precision: 1. |
| `sensor` | `power_rad2` | Radiator 2 pump power | `<base>/state/power_rad2` | — | `device_class: power`, unit: `W`, precision: 1. |
| `sensor` | `power_mixer` | Mixer motor power | `<base>/state/power_mixer` | — | `device_class: power`, unit: `W`, precision: 1. |
| `sensor` | `mixer_state` | Mixer state | `<base>/state/mixer_state` | — | `device_class: enum`, options: `["idle", "opening", "closing"]`. |
| `sensor` | `temp_boiler` | Boiler supply temperature | `<base>/state/temp_boiler` | — | `device_class: temperature`, unit: `°C`, precision: 1. |
| `sensor` | `temp_return` | Floor return temperature | `<base>/state/temp_return` | — | `device_class: temperature`, unit: `°C`, precision: 1. |
| `sensor` | `temp_mix` | Mixer forward temperature | `<base>/state/temp_mix` | — | `device_class: temperature`, unit: `°C`, precision: 1. |
| `number` | `floor_setpoint` | Floor setpoint | `<base>/state/floor_setpoint` | `<base>/cmd/floor_setpoint`| Range: 5.0–80.0 °C, step: 0.5 °C, mode: `box`, unit: `°C`. |
| `sensor` | `system_error_code` | System error code | `<base>/state/system_error_code` | — | Diagnostic text sensor. Numeric standardized code; `0` means no error. Published only when the diagnostic posture changes. |
| `sensor` | `system_error_text` | System error text | `<base>/state/system_error_text` | — | Diagnostic text sensor. Human-readable code name plus active warning/alarm flags; `No error` when healthy. |

#### Retirement of old entities

The switch entities `heater`, `pump_floor`, `pump_rad1`, `pump_rad2` and the
binary sensors `heater_virtual`, `heater_physical`, `pump_floor_virtual`,
`pump_floor_physical`, `pump_rad1_physical`, `pump_rad2_physical` are no longer
published. On every discovery cycle the controller publishes an **empty retained
payload** to their discovery topics, which is the Home Assistant contract for
deleting an entity. This keeps upgrades clean without manual HA cleanup.

---

## 3. Inbound Command Interface (`<base>/cmd/#`)

The controller subscribes with QoS 1 to `<base>/cmd/#`. Incoming payloads are parsed as ASCII text (case-insensitive where applicable).

### 3.1 Actuator Demand Commands

Accepted payloads: `"ON"`, `"OFF"`, `"1"`, `"0"`, `"true"`, `"false"`.

| Topic | Associated State Variable | Processing Contract & Keep-Alive Semantics |
| :--- | :--- | :--- |
| `<base>/cmd/heating_loop_1` | `state.pump_rad1_intended` | Updates radiator circuit 1 demand. Every accepted command, including a repeated `ON`, resets `demand_age_s[CIRCUIT_RAD1] = 0`. HA must repeat the `ON` periodically (e.g. every 5 min) while heating is active. |
| `<base>/cmd/heating_loop_2` | `state.pump_rad2_intended` | Updates radiator circuit 2 demand and resets `demand_age_s[CIRCUIT_RAD2]`. Same repeated-`ON` keep-alive. |
| `<base>/cmd/floor_heating` | `state.pump_floor_intended` | Updates floor circuit demand and resets `demand_age_s[CIRCUIT_FLOOR]`. Same repeated-`ON` keep-alive. |

There is **no heater command**. In normal operation L2 derives
`heater_intended = floor demand || loop 1 demand || loop 2 demand` after the
keep-alive and coordination rules. Test Mode may drive the heater manually
through the local portal.

#### Keep-alive timeout contract

When a circuit's `ON` intent is not refreshed for `demand_timeout_s`:

1. That circuit's effective `*_demand` drops to `false`; `*_intended` stays
   `true` so a later `ON` re-arms it immediately.
2. `divergence_reason = DIV_DEMAND_LOSS` and `system_error_code = 301`
   (`ERR_HA_KEEPALIVE_TIMEOUT`) while any demand is stale.
3. The derived heater call drops. The pump completes its board-owned
   `pump_stop_after_heating_s` after-run, then stops.
4. A repeated `ON` clears the stale state and error `301` on the next tick.

#### Summer Mode Rejection Contract:
If `config.summer_mode == true`, any incoming `ON` command targeting the three heating-loop switches is **strictly rejected**:
1. An error event (code `1808`) is written to the controller event log.
2. The corresponding `*_intended` state variable remains `false`.
3. The switch state in Home Assistant reports `OFF`, producing an immediate command-state mismatch notification in HA automations.

### 3.2 Dynamic Setpoint Command

* **Topic:** `<base>/cmd/floor_setpoint`
* **Payload:** Decimal floating-point string (e.g. `"36.5"`).
* **Validation:** Bounds checked against `[5.0, 80.0] °C`.
* **Action:** Updates `config_mutable().floor_setpoint_shadow` and emits event log code `1804`. Values outside valid bounds are discarded with error code `1804`.

---

## 4. Outbound State Publishing Contract

All outbound state topics are published under `<base>/state/` with **QoS 1** and **retained = true**, ensuring newly connected clients or restarting Home Assistant instances immediately acquire accurate state.

Publishing occurs during `mqtt::tick()` (1000 ms cadence) and executes only when values change beyond established thresholds.

| Topic | Payload Format | Publishing Trigger / Epsilon |
| :--- | :--- | :--- |
| `<base>/state/heating_loop_1` | `"ON"` / `"OFF"` | On effective-demand transition (`pump_rad1_demand`). |
| `<base>/state/heating_loop_2` | `"ON"` / `"OFF"` | On effective-demand transition (`pump_rad2_demand`). |
| `<base>/state/floor_heating` | `"ON"` / `"OFF"` | On effective-demand transition (`pump_floor_demand`). |
| `<base>/state/pump_1_physical` | `"ON"` / `"OFF"` | On physical relay transition (`pump_rad1_phys`). |
| `<base>/state/pump_2_physical` | `"ON"` / `"OFF"` | On physical relay transition (`pump_rad2_phys`). |
| `<base>/state/floor_pump_physical` | `"ON"` / `"OFF"` | On physical relay transition (`pump_floor_phys`). |
| `<base>/state/heater_physical` | `"ON"` / `"OFF"` | On physical relay transition (`heater_phys`). |
| `<base>/state/power_floor` | Float string: `"%.2f"` (W) | Change $\ge 0.05\ \text{W}$ (`kFloatPublishEpsilon`). |
| `<base>/state/power_rad1` | Float string: `"%.2f"` (W) | Change $\ge 0.05\ \text{W}$. |
| `<base>/state/power_rad2` | Float string: `"%.2f"` (W) | Change $\ge 0.05\ \text{W}$. |
| `<base>/state/power_mixer` | Float string: `"%.2f"` (W) | Change $\ge 0.05\ \text{W}$. |
| `<base>/state/mixer_state` | `"idle"`, `"opening"`, `"closing"` | On direction or motion transition. |
| `<base>/state/temp_boiler` | Float string: `"%.2f"` or `"unknown"` | Change $\ge 0.05\ \text{K}$ or validity transition. |
| `<base>/state/temp_return` | Float string: `"%.2f"` or `"unknown"` | Change $\ge 0.05\ \text{K}$ or validity transition. |
| `<base>/state/temp_mix` | Float string: `"%.2f"` or `"unknown"` | Change $\ge 0.05\ \text{K}$ or validity transition. |
| `<base>/state/floor_setpoint` | Float string: `"%.2f"` (°C) | Change $\ge 0.05\ \text{K}$. |

---

## 5. Dynamic Inbound Sensor Discovery & Parsing

Rather than requiring brittle, hardcoded topic configurations for external sensors, the controller implements a **dynamic MQTT consumer** that automatically binds to external entities by reading Home Assistant discovery configurations.

### 5.1 Mixer Position Meter (`pos_encoder`)

The motorized valve can be paired with an external position encoder (such as a Wi-Fi accelerometer/inclinometer mounted on the valve lever).

1. **Auto-Discovery Binding:**
   * The controller subscribes to `homeassistant/sensor/<mixer_meter_device>/<mixer_meter_field>/config`.
   * When received, it extracts `state_topic` using `ArduinoJson` and subscribes to the discovered topic.
2. **Direct Topic Override:**
   * If `config.mixer_meter_topic` is specified, auto-discovery is bypassed and the controller subscribes directly to this topic.
3. **Payload Processing:**
   * Incoming payload is parsed as a decimal float.
   * Scaled according to `config.mixer_meter_scale`:
     * `0`: Percent ($0$–$100$) $\implies$ divided by $100.0$.
     * `1`: Normalized fraction ($0.0$–$1.0$).
   * Clamped to $[0.0, 1.0]$.
   * Stores `pos_encoder = value`, updates `pos_encoder_ms = millis()`, and sets `pos_encoder_valid = true`.

### 5.2 Boiler DHW Heating Notification (`dhw_active`)

To handle boiler Domestic Hot Water time-sharing without tripping false stagnation errors, the controller consumes the boiler's DHW status (e.g. from an EMS-ESP gateway).

1. **Auto-Discovery Binding:**
   * Subscribes to `homeassistant/binary_sensor/<dhw_meter_device>/<dhw_meter_field>/config` and `homeassistant/sensor/...`.
   * Dynamically extracts `state_topic` and subscribes.
2. **Direct Topic Override:**
   * Configurable via `config.dhw_meter_topic`.
3. **Complex Payload & EMS-ESP Parsing:**
   * The parser (`extract_payload_field()`) extracts the target attribute specified by `config.dhw_meter_field` (e.g. `"dhw.charging"` or `"tapwateractive"`).
   * Supports both simple strings (`"ON"` / `"OFF"`, `"1"` / `"0"`) and nested YAML / colon-delimited telegram payloads.
   * Sets `state.dhw_active = active`.

---

## 6. Diagnostics & Extended Interface Evolution

### 6.1 Implemented Diagnostic Publishing

* **Topic:** `<base>/test`
* **Trigger:** Invoked via HTTP POST `/api/mqtt/test` or internal diagnostic check.
* **Payload:** JSON string reporting uptime and current IP:
  ```json
  {"uptime": 14205, "ip": "192.168.2.14"}
  ```

### 6.2 Telemetry Log Streaming (Implemented)

Event and slow logs are kept only in **RAM circular buffers** and are never written to flash. When `mqtt_log_enabled` is set, new records are published as JSON lines:

* `<base>/log/slow` — periodic telemetry samples (default every 10 s); override the full topic with `mqtt_log_slow_topic`.
* `<base>/log/event` — event records; override the full topic with `mqtt_log_event_topic`.

Each stream carries a `seq` field and an uptime timestamp (`ts`); a wall-clock field (`wall`) is added when NTP is synchronized. Non-retained publishes keep the broker history clean. If the broker is unavailable, logging continues in RAM and the retained backlog is sent after reconnection. With streaming disabled, the same buffers remain available through the web portal (`/api/log/events`, `/api/log/slow`).

### 6.3 Planned Future Evolution

The following topics are reserved for implementation in Phase 2 and Phase 3:

1. **Direct Mode Switching (`cmd/mode`):**
   * *Target Topic:* `<base>/cmd/mode`
   * *Payloads:* `"heat"`, `"summer"`
   * *Behavior:* Toggles `config.summer_mode` directly from Home Assistant.
2. **Observer Layer 3 Tuning Ingestion:**
   * *Target Topic:* `<base>/config/tuning/set`
   * *Payload:* JSON object containing validated loop coefficients (`delay_s`, `gain`, `kp`, `ki`, `ff_boiler`, `ff_return`, etc.).
