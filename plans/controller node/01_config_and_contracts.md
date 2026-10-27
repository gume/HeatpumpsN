# 01 — Configuration, Parameter Ownership & State Contracts

**Parent:** `00_index_and_architecture.md`  
**Scope:** Canonical data structures, configuration parameters (`struct Config`), runtime state contracts (`struct SystemState`), staged demand reporting semantics (`_intended` / `_demand` / `_requested` / `_phys`), and standard divergence reason codes used across all modules (`02`–`06`).

---

## 1. Configuration & Parameter Ownership (`struct Config`)

Per Master Plan §7, parameters are partitioned into **three disjoint ownership categories**. Every parameter belongs to exactly one owner, and safety-relevant parameters in any category are strictly bounded so no actor can configure an unsafe state.

```c
struct Config {
  // =========================================================================
  // CATEGORY 1: Long-Term Configuration (Owner: Human via Web Dashboard ONLY)
  // =========================================================================
  // --- Provisioning (settable via WiFi manager portal / dashboard) ---
  char     wifi_ssid[33], wifi_password[64];
  char     ap_ssid[33], ap_password[32];        // fallback AP
  char     mqtt_host[64]; uint16_t mqtt_port;
  char     mqtt_user[33], mqtt_pass[65];
  char     mqtt_base_topic[48];
  // Optional streaming of the RAM log buffers. Empty topics use the
  // base-derived defaults <base>/log/slow and <base>/log/event.
  bool     mqtt_log_enabled;       // false = keep logs in RAM only
  char     mqtt_log_slow_topic[96];
  char     mqtt_log_event_topic[96];

  // --- Safety & Supervision Bounds ---
  float    t_max_hard;             // absolute floor cutoff, e.g. 50.0 degC
  float    t_max_warn;             // early-warning band, e.g. 45.0 degC
  float    t_overtemp_hysteresis;  // recovery band below t_max_warn, e.g. 2.0 K
  float    grad_limit;             // max dT/dt (K/s), e.g. 0.5
  float    mix_safe_pos;           // 0.0 = closed (confirmed safe position)
  uint8_t  mix_max_open_pct;       // L1-enforced opening ceiling, 10-100 %, default 85 %

  // --- Pump current measurement (shared ACS712/CT calibration) ---
  float    pump_current_w_per_v;   // slope, W per Vrms above the offset
  float    pump_current_offset_v;  // RMS voltage at zero current
  bool     pump_power_check_enabled; // false = measure only, no dry-run/welded trip

  // --- Mixer operating window ---
  uint8_t  mix_min_open_pct;       // normal floor-demand minimum, 0-100 %, default 40 %
  bool     mix_max_dynamic;        // enable the two-point maximum
  uint8_t  mix_max_below_pct;      // dynamic ceiling below the switch point, default 90 %
  uint8_t  mix_max_above_pct;      // dynamic ceiling above the switch point, default 70 %
  float    mix_max_switch_c;       // boiler-side switching temperature, default 50 degC
  float    mix_max_hysteresis_c;   // hysteresis around the switch point, default 2 K

  // Soft setpoint+delta reaction (warning-only; L1 hard limit stays authoritative)
  float    mix_overshoot_delta_c;     // react at setpoint + delta, default 3 K
  float    mix_overshoot_recovery_c;  // release below setpoint - recovery, default 1 K
  uint8_t  mix_overshoot_target_pct;  // opening held during the reaction, default 40 %

  // --- Sensor role plausibility (heating only, warning-only) ---
  bool     sensor_role_check_enabled; // default true
  float    sensor_role_tolerance_c;   // allowed inversion, default 3 K

  // --- Optional inferred DHW ---
  bool     dhw_infer_enabled;      // default false; meter-based DHW is preferred
  float    dhw_infer_drop_c;       // boiler drop that starts an inference, default 5 K
  uint32_t dhw_infer_window_s;     // how long a peak stays a valid baseline, default 120 s
  float    dhw_infer_recovery_c;   // recovery band that ends the inference, default 3 K
  uint32_t dhw_infer_max_s;        // maximum plausible charge, default 1800 s

  uint32_t boiler_min_off_ms;      // anti-short-cycle backstop
  uint32_t demand_timeout_s;       // per-circuit HA switch keep-alive timeout (e.g., refresh_time * 2)
  uint32_t pump_stop_after_heating_s; // board-owned pump after-run per circuit after demand ends
  uint32_t long_pause_s;           // pause length before heater_phys off considered
  uint32_t anti_seize_interval_s;  // periodic summer exercise interval (~30 days)
  uint32_t test_mode_max_s;        // max duration for dashboard Test mode (e.g. 7200 s)
  uint8_t  sensor_error_threshold; // consecutive bad reads before persistent fault (e.g. 3)

  // --- Hardware & Actuator Constants ---
  float    setpoint_default;       // fallback floor setpoint if none in NVS/HA
  float    deadband_pos, deadband_temp;
  uint32_t travel_time_ms, tick_ms; // full-stroke time (dead reckoning) and control tick
  // The mixer is a continuous target-position servo, not a pulse train, so
  // there are no pulse-width or cooldown parameters.
  uint32_t temp_poll_interval_ms;  // steady 1-wire polling interval (e.g. 1000 ms)
  float    recalc_threshold;       // enc/DR divergence threshold for warning/re-sync
  uint32_t encoder_check_interval_s; // interval for periodic encoder check (e.g. 3600 s)
  uint32_t encoder_check_pulses;   // movement count for periodic encoder check (e.g. 100)
  uint32_t encoder_staleness_s;    // max age of pos_encoder before flagged stale (e.g. 300 s)
  uint8_t  pos_source;             // POS_SRC_HA_GENERIC (accelerometer/magnet) or POS_SRC_I2C

  // --- I/O Assignment (Pins TBD pending board layout) ---
  uint8_t  gpio_pump_floor;        // Relay GPIO (TBD)
  uint8_t  gpio_pump_rad1;         // Relay GPIO (TBD)
  uint8_t  gpio_pump_rad2;         // Relay GPIO (TBD)
  uint8_t  gpio_heater;            // Relay GPIO (TBD)
  uint8_t  gpio_mix_open;          // Triac/Relay GPIO (TBD)
  uint8_t  gpio_mix_close;         // Triac/Relay GPIO (TBD)
  uint8_t  gpio_onewire;           // 1-Wire DS18B20 bus GPIO (TBD)
  uint8_t  gpio_led_data;          // Addressable RGB LED data GPIO (TBD, WS2812B/SK6812)
  uint8_t  num_leds;               // Addressable LED count (e.g. 5, see 02_hardware_and_l0.md)
  bool     has_pump_power[3];      // optional power sensors [floor, rad1, rad2]
  uint8_t  i2c_sda, i2c_scl;       // I2C bus (PCF8574 / future mixer encoder)
  char     sensor_id_boiler[17], sensor_id_ret[17], sensor_id_mix[17];

  // --- Output driver / port expander --------------------------------------
  // Runtime-selectable so one firmware image can drive direct GPIOs, the
  // legacy 74HC595 carrier or a PCF8574 board (including a custom address).
  uint8_t  output_expander;        // 0 = direct, 1 = 74HC595, 2 = PCF8574
  uint8_t  expander_address;       // 7-bit I2C address, default 0x20
  uint8_t  expander_data_pin;      // 74HC595 SER/DS
  uint8_t  expander_clock_pin;     // 74HC595 SRCLK/SHCP
  uint8_t  expander_latch_pin;     // 74HC595 RCLK/STCP
  uint8_t  expander_oe_pin;        // 74HC595 /OE, 255 when hard-wired
  bool     expander_oe_active_low; // true = low enables the 595 outputs
  bool     output_active_low;      // true = logical ON drives the pin low
  uint8_t  expander_bit_heater;    // expander bit 0-7 for each logical channel
  uint8_t  expander_bit_pump_rad1;
  uint8_t  expander_bit_pump_rad2;
  uint8_t  expander_bit_pump_floor;
  uint8_t  expander_bit_mix_direction;
  uint8_t  expander_bit_mix_motion;
  uint8_t  expander_bit_mux_s0;
  uint8_t  expander_bit_mux_s1;

  // =========================================================================
  // CATEGORY 2: Dynamic Heating Parameters (Owner: Home Assistant, shadowed)
  // =========================================================================
  float    floor_setpoint_shadow;  // ideal floor forwarding temp from HA
  bool     summer_mode;            // true = Summer (dry) mode, false = Heat mode

  // =========================================================================
  // CATEGORY 3: Control Loop Tuning (Owner: Observer ONLY, L3-validated)
  // =========================================================================
  float    delay_s, gain;
  float    ff_boiler, ff_return;
  float    kp, ki;
  float    rad_coupling_ff;        // feedforward compensation when rad pumps start
  uint32_t start_flush_ms;         // floor pump start/restart flush duration, e.g. 30 s
  float    flush_max_pos;          // mixer position cap during flush, e.g. 0.20 (20%)
  float    flush_delta_t_ok;       // optional stabilization criterion for early flush end
  float    min_useful_delta_t;     // floor delta_T collapse threshold (T_mix_out - T_return_floor)
  uint32_t floor_pause_duration_s; // planned pause duration when floor delta_T collapses (optimized by observer)
};
```

### 1.1 Parameter Ownership & Persistence Rules

1. **Category 1 — Long-Term Configuration:** Editable *only* by a human on the local Web Dashboard (and WiFi manager portal); persisted in NVS.
2. **Category 2 — Dynamic Heating Parameters:** Commanded dynamically by **Home Assistant** (`floor_setpoint`, `summer_mode`). The controller persists the last valid value in NVS as a shadow/fallback (`setpoint_default` is used only before the first valid HA setpoint ever arrives or if NVS is blank).
3. **Category 3 — Control Loop Tuning:** Computed and pushed *only* by the **Observer** into L3. L3 validates every parameter against hard compiled-in safety/plausibility bounds before adoption and NVS persistence. Not exposed for manual editing on human UIs.

**NVS format and integrity:** the whole `Config` is stored as one blob with a magic value, format version, exact struct size, and a CRC32 checksum. Any blob that fails validation is discarded and compiled defaults are used. Older formats are migrated by copying their unchanged prefix onto current defaults: v1 (1288-byte prefix) and v2 (1348 bytes) are supported, and the appended fields (currently the output-driver block in v3) receive valid defaults. A CRC/size failure in a current-format blob latches `WARN_NVS_CORRUPTED` / `ERR_NVS_DATA_CORRUPTED` so the condition is visible in Home Assistant and the event log; an older-format blob is migrated (or discarded if it predates a supported prefix). The latch is held for the current boot and clears on the next boot once all persisted blobs validate; saving settings rewrites the config blob but does not clear the live flag, so unrelated counter or anti-seize damage cannot be hidden by a config save. Relay counters and the anti-seize schedule use the same CRC32-protected blob pattern.

---

## 2. Demand/Request Contract (`*_intended` vs. `*_demand` vs. `*_requested` vs. `*_phys`)

To maintain clean separation between external commands (L4), the keep-alive and
coordination logic (L2), safety overrides (L1), and hardware pins (L0), every
switched actuator uses a staged state model:

| State Variable | Owner / Writer | Meaning & HA Reporting Contract |
| :--- | :--- | :--- |
| `pump_floor_intended`<br>`pump_rad1_intended`<br>`pump_rad2_intended` | **L4** (HA Command Intake / Test Mode) | The last accepted per-circuit command. When HA commands a heating loop ON, `*_intended` remains ON even if L2 or L1 temporarily pauses the physical relay (due to a missed keep-alive, $\Delta T$ collapse, L1 overtemp, anti-short-cycle, or DHW priority). This prevents HA from falsely assuming a communication failure.<br>*Exception (Summer Mode):* If HA commands a switch ON while in Summer Mode, L4 rejects the command and keeps `*_intended = OFF` to intentionally trigger an error/mismatch in HA. |
| `pump_floor_demand`<br>`pump_rad1_demand`<br>`pump_rad2_demand` | **L2** (Keep-alive) | The effective heating demand: `*_intended &&` the circuit's repeated ON command is still fresh. A missed keep-alive drops the demand while leaving the raw intent set, so a later ON re-arms it. In Test Mode the demand equals the intent. |
| `pump_floor_requested`<br>`pump_rad1_requested`<br>`pump_rad2_requested` | **L2** (Control & Coordination) | The state requested after warm-up flush rules, $\Delta T$-collapse pauses, `long_pause_s` cutoffs and the board-owned pump after-run, passed down to L1. |
| `heater_intended`<br>`heater_requested` | **L2** (derived, normal mode) / **L4** (Test Mode) | In normal operation the heater has no HA command: L2 sets `heater_intended = requested` from `req_floor \|\| req_rad1 \|\| req_rad2` (after coordination, before the pump-only after-run). Test Mode may still drive `heater_intended` manually. |
| `heater_phys`<br>`pump_floor_phys`<br>`pump_rad1_phys`<br>`pump_rad2_phys` | **L1** (Safety Arbiter) $\rightarrow$ **L0** (GPIO) | The final safety-arbitrated physical relay command written to GPIO pins by L0 and reported back to HA via the physical binary sensors for true hardware monitoring. |

---

## 3. Shared Runtime State (`struct SystemState`)

All tasks communicate through `SystemState` using lock-free atomic snapshot mailboxes across cores (`T_safety/T_control` on Core 1 publish snapshots to `T_comms/T_logger` on Core 0) so safety arbitration never blocks. Every sensor reading carries a monotonic timestamp and validity status.

```c
typedef enum {
  MODE_HEAT = 0,
  MODE_SUMMER,
  MODE_ANTI_SEIZE,
  MODE_TEST_OVERRIDE
} OperationalMode;

typedef enum {
  POS_SRC_HA_GENERIC = 0,       // forwarded from HA (e.g. WiFi accelerometer/magnet)
  POS_SRC_I2C = 1               // local direct I2C encoder (e.g. AS5600)
} PositionSource;

typedef enum {
  FLUSH_IDLE = 0,
  FLUSH_ACTIVE,
  FLUSH_PAUSED_DELTA_T
} FlushState;

typedef enum {
  DIV_NONE = 0,
  DIV_FLOOR_DELTA_T_COLLAPSED,
  DIV_OVERTEMP_PAUSE,
  DIV_LONG_PAUSE,
  DIV_ANTI_SHORT_CYCLE,
  DIV_DEMAND_LOSS,
  DIV_AFTER_RUN,             // board-managed pump overrun after a demand ends
  DIV_DHW_PRIORITY_ACTIVE
} DivergenceReason;

// Per-circuit runtime indexes. The order matches pump_power_w[3] and the
// current channels (floor, radiator 1, radiator 2).
typedef enum {
  CIRCUIT_FLOOR = 0,
  CIRCUIT_RAD1,
  CIRCUIT_RAD2,
  CIRCUIT_COUNT
} CircuitIndex;

// Bitmask constants for SystemState.active_alarms
#define ALARM_OVERTEMP_HARD        (1u << 0) // T_mix_out >= t_max_hard trip
#define WARN_OVERTEMP_HIGH         (1u << 1) // T_mix_out >= t_max_warn inhibit open
#define WARN_GRADIENT_CUTOFF       (1u << 2) // dT/dt > grad_limit abort open
#define ALARM_SENSOR_FAULT_MIX     (1u << 3) // T_mix_out persistent fault (critical)
#define WARN_SENSOR_FAULT_RET      (1u << 4) // T_return_floor persistent fault
#define WARN_SENSOR_FAULT_BOILER   (1u << 5) // T_boiler persistent fault
#define WARN_HEATER_NO_PUMP        (1u << 6) // heater ON with all pumps OFF
#define WARN_ENCODER_STALE         (1u << 7) // pos_encoder missing or stale on check
#define WARN_ENCODER_DIVERGED      (1u << 8) // |dr - enc| > recalc_threshold
#define WARN_PUMP_DRY_RUN          (1u << 9) // pump_phys ON with power ~ 0 W
#define ALARM_WELDED_RELAY         (1u << 10)// pump_phys OFF with power > 0 W
#define WARN_NVS_CORRUPTED         (1u << 11)// NVS CRC error; defaults loaded

// Standard System Error Codes (SystemState.system_error_code)
#define ERR_OK                     0
#define ERR_L1_HARD_OVERTEMP       101
#define ERR_SENSOR_MIX_LOST        201
#define ERR_SENSOR_RET_LOST        202
#define ERR_SENSOR_BOILER_LOST     203
#define ERR_HA_KEEPALIVE_TIMEOUT   301
#define ERR_RELAY_FAULT_WELDED     401
#define ERR_NVS_DATA_CORRUPTED     501
#define ERR_SENSOR_ROLE_SUSPECT    603
#define ERR_DHW_INFER_TIMEOUT      604

struct SensorReading {
  float    value_c;
  float    gradient_k_per_s;   // computed at L0 from steady 1-wire polling
  uint32_t last_valid_ms;      // monotonic uptime of last valid sample
  uint8_t  consecutive_errors; // transient glitch counter at L0
  bool     is_valid;           // true if healthy, false on persistent fault
};

struct SystemState {
  // --- Authoritative Clock ---
  uint32_t uptime_ms;
  uint64_t wall_time_epoch_s;  // best-effort NTP annotation for logs

  // --- Mode & External Status ---
  OperationalMode mode;
  bool     dhw_active;         // combined priority: meter OR inference
  bool     dhw_meter_active;   // raw HA/MQTT DHW signal
  bool     dhw_inferred;       // inferred from a boiler-temperature drop
  uint32_t test_mode_rem_s;    // remaining seconds in Test Override mode

  // --- Temperatures (L0 -> L1/L2) ---
  struct SensorReading T_boiler;
  struct SensorReading T_return_floor;
  struct SensorReading T_mix_out;
  bool     t_ret_flush_valid;  // false during floor warm-up flush phase

  // --- Mixer Position & Actuation State ---
  float    pos_est_dr;         // internal dead-reckoned position [0.0 .. 1.0]
  float    pos_encoder;        // external abstract encoder position [0.0 .. 1.0]
  uint32_t pos_encoder_ms;     // monotonic timestamp of last encoder update
  bool     pos_encoder_valid;
  float    integrator_val;     // L2 mixer PI integrator state
  float    mix_target_pos;     // desired position; the only mixer command
  float    mix_ceiling;        // effective L1 ceiling (absolute vs dynamic minimum)
  bool     mix_overshoot;      // soft setpoint+delta reaction latched
  bool     mix_motion;         // L1 -> L0: motor energized right now
  int8_t   mix_dir;            // L1 -> L0: +1 opening, -1 closing, 0 idle
  bool     mix_emergency;      // emergency close latched; other writers locked out
  int8_t   mix_free_run_dir;   // non-zero: homing/sweep, ignore target and limits
  uint32_t mix_free_run_until_ms; // free-run safety deadline

  // --- Relays: Intended (L4), Demand/Requested (L2), Physical (L1 -> L0) ---
  bool     heater_intended,     heater_requested,     heater_phys;
  bool     pump_floor_intended, pump_floor_demand, pump_floor_requested, pump_floor_phys;
  bool     pump_rad1_intended,  pump_rad1_demand,  pump_rad1_requested,  pump_rad1_phys;
  bool     pump_rad2_intended,  pump_rad2_demand,  pump_rad2_requested,  pump_rad2_phys;

  // --- Optional Feedback & Supervision Timers ---
  float    pump_power_w[3];    // [floor, rad1, rad2] power in Watts (if present)
  float    mixer_power_w;      // mixer motor power in Watts (if present)
  uint32_t demand_age_s[CIRCUIT_COUNT];   // since last accepted circuit ON
  uint32_t after_run_age_s[CIRCUIT_COUNT]; // board pump after-run per circuit

  // --- Diagnostics, Alarms & Divergence ---
  DivergenceReason divergence_reason;
  uint16_t system_error_code;  // 0 = ERR_OK (normal operation)
  uint32_t active_alarms;      // bitmask of active L1/L0 alarms & warnings
  FlushState flush_state;      // FLUSH_IDLE, FLUSH_ACTIVE, FLUSH_PAUSED_DELTA_T
};
```

---

## 4. Canonical Divergence Reason Codes & Priority

Whenever a circuit's effective demand differs from its physical relay state (or when heat delivery is suspended by external boiler state), `divergence_reason` must be set to one of the canonical values below and published to HA and the event log. The derived heater call is reported through `heater_requested` / `heater_phys`; a normal pump after-run is reported as `DIV_AFTER_RUN`, not as an error.

### 4.1 Evaluation Priority Hierarchy
When multiple divergence or override conditions coincide, `divergence_reason` must report the condition with the **highest priority**:

1. **`DIV_OVERTEMP_PAUSE`** (Priority 1 — Highest): L1 hard safety cutoff active ($T_{\text{mix\_out}} \ge t_{\text{max\_hard}}$); `pump_floor_phys` forced `OFF` and mixer driven to `0.0`.
2. **`DIV_DEMAND_LOSS`** (Priority 2): one or more per-circuit keep-alives missed beyond `demand_timeout_s`; the effective `*_demand` and the derived heater call drop while `*_intended` stays set (`ERR_HA_KEEPALIVE_TIMEOUT`).
3. **`DIV_ANTI_SHORT_CYCLE`** (Priority 3): `heater_phys` held `OFF` until `boiler_min_off_ms` expires (`heater_requested = ON`).
4. **`DIV_DHW_PRIORITY_ACTIVE`** (Priority 4): `heater_phys` remains `ON`, but HA reported `dhw_active == true`; L2 integrator is frozen and stagnation alarms are suppressed while the boiler heats domestic hot water.
5. **`DIV_FLOOR_DELTA_T_COLLAPSED`** (Priority 5): Floor loop $\Delta T < \text{min\_useful\_delta\_t}$; floor pump (and heater, if no radiators are active) paused for `floor_pause_duration_s` (`pump_floor_intended = ON`, `pump_floor_phys = OFF`).
6. **`DIV_LONG_PAUSE`** (Priority 6): `heater_phys` turned `OFF` after running with all three pumps `OFF` for longer than `long_pause_s` (`heater_requested = ON`).
7. **`DIV_AFTER_RUN`** (Priority 7): a circuit demand ended and that circuit's pump is still physically running for the board-owned `pump_stop_after_heating_s` after-run. Reported only when no higher-priority condition is active; it is normal operation, not an error.
8. **`DIV_NONE`** (Priority 8 — Normal): All physical relays match their coordinated requests and heating operates normally.