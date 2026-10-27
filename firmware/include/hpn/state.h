#pragma once

#include <stddef.h>
#include <stdint.h>

// Shared runtime contract.
//
// Source of truth: plans/controller node/01_config_and_contracts.md (§2-§4).
// Keep enum values, alarm bits, error codes, and field names in sync with it.

typedef enum {
  MODE_HEAT = 0,
  MODE_SUMMER,
  MODE_ANTI_SEIZE,
  MODE_TEST_OVERRIDE
} OperationalMode;

typedef enum {
  POS_SRC_HA_GENERIC = 0,
  POS_SRC_I2C = 1
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
  DIV_AFTER_RUN,          // board-managed pump overrun after a demand ends
  DIV_DHW_PRIORITY_ACTIVE
} DivergenceReason;

// Per-circuit runtime indexes. The order matches pump_power_w[3] and the
// CurrentChannel enum (floor, radiator 1, radiator 2).
typedef enum {
  CIRCUIT_FLOOR = 0,
  CIRCUIT_RAD1,
  CIRCUIT_RAD2,
  CIRCUIT_COUNT
} CircuitIndex;

// Bitmask constants for SystemState.active_alarms
#define ALARM_OVERTEMP_HARD (1u << 0)
#define WARN_OVERTEMP_HIGH (1u << 1)
#define WARN_GRADIENT_CUTOFF (1u << 2)
#define ALARM_SENSOR_FAULT_MIX (1u << 3)
#define WARN_SENSOR_FAULT_RET (1u << 4)
#define WARN_SENSOR_FAULT_BOILER (1u << 5)
#define WARN_HEATER_NO_PUMP (1u << 6)
#define WARN_ENCODER_STALE (1u << 7)
#define WARN_ENCODER_DIVERGED (1u << 8)
#define WARN_PUMP_DRY_RUN (1u << 9)
#define ALARM_WELDED_RELAY (1u << 10)
#define WARN_NVS_CORRUPTED (1u << 11)
#define WARN_MIX_MOTION_OVERRUN (1u << 12)
#define WARN_MIX_NO_RESPONSE (1u << 13)
#define WARN_MIX_OVERSHOOT (1u << 14)
#define WARN_SENSOR_ROLE_SUSPECT (1u << 15)
#define WARN_DHW_INFER_TIMEOUT (1u << 16)

// Standard system error codes (SystemState.system_error_code)
#define ERR_OK 0
#define ERR_L1_HARD_OVERTEMP 101
#define ERR_SENSOR_MIX_LOST 201
#define ERR_SENSOR_RET_LOST 202
#define ERR_SENSOR_BOILER_LOST 203
#define ERR_HA_KEEPALIVE_TIMEOUT 301
#define ERR_RELAY_FAULT_WELDED 401
#define ERR_NVS_DATA_CORRUPTED 501
#define ERR_SENSOR_ROLE_SUSPECT 603
#define ERR_DHW_INFER_TIMEOUT 604

struct SensorReading {
  float value_c;
  float gradient_k_per_s;
  uint32_t last_valid_ms;
  uint8_t consecutive_errors;
  bool is_valid;
};

struct SystemState {
  // --- Authoritative clock --------------------------------------------------
  uint32_t uptime_ms;
  uint64_t wall_time_epoch_s;

  // --- Mode & external status ----------------------------------------------
  OperationalMode mode;
  bool dhw_active;        // combined priority signal used by control
  bool dhw_meter_active;  // raw signal from the configured MQTT DHW meter
  bool dhw_inferred;      // inferred from a boiler-temperature drop
  uint32_t test_mode_rem_s;

  // --- Temperatures (L0 -> L1/L2) ------------------------------------------
  struct SensorReading T_boiler;
  struct SensorReading T_return_floor;
  struct SensorReading T_mix_out;
  bool t_ret_flush_valid;

  // --- Mixer position & actuation state ------------------------------------
  float pos_est_dr;
  float pos_encoder;
  uint32_t pos_encoder_ms;
  bool pos_encoder_valid;
  float integrator_val;
  float mix_target_pos;            // desired position; the only mixer command
  float mix_ceiling;               // effective L1 opening ceiling (0-1)
  bool mix_overshoot;              // soft setpoint+delta reaction latched
  bool mix_motion;                 // motor energized right now (L1 -> L0)
  int8_t mix_dir;                  // +1 opening, -1 closing, 0 idle
  bool mix_emergency;              // emergency close latched; writers locked out
  int8_t mix_free_run_dir;         // non-zero: homing/sweep, ignore target/limits
  uint32_t mix_free_run_until_ms;  // free-run safety deadline

  // --- Relays: Intended (L4), Demand (L2), Requested (L2), Physical (L1 -> L0)
  //
  // Heating switches own the circuit demand. `*_intended` is the last accepted
  // HA/Test command; `*_demand` is the effective request after the per-circuit
  // keep-alive check (and equals `*_intended` in Test Mode). The heater has no
  // HA switch: in normal operation L2 derives `heater_intended` from the three
  // circuit demands. Test Mode may still drive `heater_intended` manually.
  bool heater_intended, heater_requested, heater_phys;
  bool pump_floor_intended, pump_floor_demand, pump_floor_requested, pump_floor_phys;
  bool pump_rad1_intended, pump_rad1_demand, pump_rad1_requested, pump_rad1_phys;
  bool pump_rad2_intended, pump_rad2_demand, pump_rad2_requested, pump_rad2_phys;

  // --- Optional feedback & supervision timers ------------------------------
  float pump_power_w[3];
  float mixer_power_w;
  // Indexed by CircuitIndex. The demand age is reset by every accepted ON
  // command (the repeated command is the keep-alive). The after-run age starts
  // when a circuit demand ends and is owned by the board.
  uint32_t demand_age_s[CIRCUIT_COUNT];
  uint32_t after_run_age_s[CIRCUIT_COUNT];

  // --- Diagnostics, alarms & divergence ------------------------------------
  DivergenceReason divergence_reason;
  uint16_t system_error_code;
  uint32_t active_alarms;
  FlushState flush_state;
};

// Boot-safe zero state: everything off, no valid sensors, no alarms.
void state_init(SystemState& state);

// Human-readable names for the standardized error code, one alarm bit and the
// current diagnostic posture. format_error_text() returns "No error" when the
// code is ERR_OK and no warning/alarm flags are set; otherwise it combines the
// code name with active flags and any state detail worth including.
const char* error_code_name(uint16_t code);
const char* alarm_name(uint32_t bit);
void format_error_text(const SystemState& state, char* out, size_t out_size);
