#pragma once

#include <stdint.h>

// Sentinel for "GPIO not assigned yet". Plans 02 still marks most pins TBD.
#define HPN_GPIO_UNASSIGNED 0xFF

// Mixer actuator wiring. The two logical channels are always open/direction
// and close/motion; this setting tells L0 how the carrier exposes them.
enum MixerWiring : uint8_t {
  MIX_WIRING_DIRECTION_MOTION = 0,  // HomeNet Heating control v0.1
  MIX_WIRING_OPEN_CLOSE = 1,        // HomeNet Heating control v1.x
};

// Output driver for the relay and mixer channels. Direct GPIO keeps the
// per-channel pins below; both expander variants share one output byte, whose
// bit assignment and polarity are also configurable.
enum OutputExpander : uint8_t {
  OUTPUT_EXPANDER_NONE = 0,    // direct GPIO pins
  OUTPUT_EXPANDER_74HC595 = 1, // HomeNet Heating control v0.1
  OUTPUT_EXPANDER_PCF8574 = 2, // HomeNet Heating control v1.x
};

// Current-measurement front end. v0.1 multiplexes four ACS712 outputs onto one
// ADC pin; v1.x reads an ADS1115 whose inputs come from the optional TLV9064
// sensor assembly. A board without current sensing reports 0 W.
enum CurrentSense : uint8_t {
  CURRENT_SENSE_NONE = 0,
  CURRENT_SENSE_MUX_ADC = 1,
  CURRENT_SENSE_ADS1115 = 2,
};

// Canonical configuration contract.
//
// Source of truth: plans/controller node/01_config_and_contracts.md (§1).
// Keep this struct in sync with the plan. Parameter ownership is documented
// there; do not add values here that bypass the three ownership categories.
struct Config {
  // =========================================================================
  // CATEGORY 1: Long-Term Configuration (Owner: Human via Web Dashboard ONLY)
  // =========================================================================

  // --- Provisioning --------------------------------------------------------
  char wifi_ssid[33];
  char wifi_password[64];
  char ap_ssid[33];
  char ap_password[32];
  char mqtt_host[64];
  uint16_t mqtt_port;
  char mqtt_user[33];
  char mqtt_pass[65];
  char mqtt_base_topic[48];

  // --- Safety & Supervision Bounds -----------------------------------------
  float t_max_hard;
  float t_max_warn;
  float t_overtemp_hysteresis;
  float grad_limit;
  float mix_safe_pos;
  uint32_t boiler_min_off_ms;
  uint32_t demand_timeout_s;
  uint32_t pump_stop_after_heating_s;
  uint32_t long_pause_s;
  uint32_t anti_seize_interval_s;
  uint32_t test_mode_max_s;
  uint8_t sensor_error_threshold;

  // --- Hardware & Actuator Constants ---------------------------------------
  float setpoint_default;
  float deadband_pos;
  float deadband_temp;
  uint32_t travel_time_ms;
  uint32_t tick_ms;
  uint32_t temp_poll_interval_ms;
  float recalc_threshold;
  uint32_t encoder_check_interval_s;
  uint32_t encoder_check_pulses;
  uint32_t encoder_staleness_s;
  uint8_t pos_source;

  // --- I/O Assignment (pins TBD pending board layout) ----------------------
  uint8_t gpio_pump_floor;
  uint8_t gpio_pump_rad1;
  uint8_t gpio_pump_rad2;
  uint8_t gpio_heater;
  uint8_t gpio_mix_open;
  uint8_t gpio_mix_close;
  uint8_t gpio_onewire;
  uint8_t gpio_led_data;
  uint8_t num_leds;
  bool has_pump_power[3];
  uint8_t i2c_sda;
  uint8_t i2c_scl;
  char sensor_id_boiler[17];
  char sensor_id_ret[17];
  char sensor_id_mix[17];

  // =========================================================================
  // CATEGORY 2: Dynamic Heating Parameters (Owner: Home Assistant, shadowed)
  // =========================================================================
  float floor_setpoint_shadow;
  bool summer_mode;

  // =========================================================================
  // CATEGORY 3: Control Loop Tuning (Owner: Observer ONLY, L3-validated)
  // =========================================================================
  float delay_s;
  float gain;
  float ff_boiler;
  float ff_return;
  float kp;
  float ki;
  float rad_coupling_ff;
  uint32_t start_flush_ms;
  float flush_max_pos;
  float flush_delta_t_ok;
  float min_useful_delta_t;
  uint32_t floor_pause_duration_s;

  // --- Network identity and time -------------------------------------------
  char hostname[33];
  bool ntp_enabled;
  char ntp_server[64];
  char timezone[40];

  // --- Carrier wiring ------------------------------------------------------
  uint8_t mixer_wiring;

  // --- MQTT meter / DHW inputs --------------------------------------------
  // The mixer position meter and DHW signal are consumed from MQTT. Device
  // and field names build the Home Assistant discovery topic; the controller
  // reads state_topic from that payload and subscribes there. The optional
  // topic overrides skip discovery and subscribe directly.
  char mixer_meter_device[33];
  char mixer_meter_field[33];
  uint8_t mixer_meter_scale;  // 0 = percent (0-100), 1 = fraction (0-1)
  char mixer_meter_topic[96];
  char dhw_meter_device[33];
  char dhw_meter_entity[33];
  char dhw_meter_field[33];
  char dhw_meter_topic[96];

  // Analysis model B intercept (K). Observer-owned; kept configurable so the
  // feedforward can be recalibrated without a firmware change.
  float mix_ff_offset;

  // --- MQTT log streaming --------------------------------------------------
  // Logs always live in RAM ring buffers. When enabled, new records are also
  // published to MQTT. An empty topic means the base-derived default
  // (<base>/log/slow or <base>/log/event).
  bool mqtt_log_enabled;
  char mqtt_log_slow_topic[96];
  char mqtt_log_event_topic[96];

  // --- Output limits -------------------------------------------------------
  // Absolute upper bound for the mixer opening, in percent (10-100). L1 never
  // drives the valve above this value; L2 and the portal clamp their targets
  // to it. In the v1 layout this field reused the former tail padding; it is
  // part of the unchanged 1288-byte prefix that v2 migration preserves.
  uint8_t mix_max_open_pct;

  // --- Pump current measurement -------------------------------------------
  // All current channels use the same ACS712/CT front end and are therefore
  // calibrated together. watts = (rms_volts - offset_v) * w_per_v. Set
  // pump_power_check_enabled=false to run without dry-run/welded protection
  // (for example before the shunt has been calibrated on the plant).
  float pump_current_w_per_v;
  float pump_current_offset_v;
  bool pump_power_check_enabled;

  // --- Mixer operating window ---------------------------------------------
  // Minimum opening while the floor circuit is in normal demand control. It
  // is never applied to closing moves, safety closes, homing or a no-demand
  // close, all of which must be able to reach 0 %.
  uint8_t mix_min_open_pct;

  // Two-point dynamic maximum. When enabled, the effective ceiling is the
  // lower of the absolute ceiling and one of the two limits, selected by the
  // boiler-side temperature around mix_max_switch_c with hysteresis.
  bool mix_max_dynamic;
  uint8_t mix_max_below_pct;
  uint8_t mix_max_above_pct;
  float mix_max_switch_c;
  float mix_max_hysteresis_c;

  // Soft over-temperature reaction. When the forward temperature reaches
  // setpoint + delta, L2 drives to mix_overshoot_target_pct and holds it until
  // the temperature falls below setpoint - recovery. This is warning-only; the
  // hard over-temperature rule remains in L1.
  float mix_overshoot_delta_c;
  float mix_overshoot_recovery_c;
  uint8_t mix_overshoot_target_pct;

  // --- Sensor role verification -------------------------------------------
  // Optional plausibility supervision: while heating is active, the boiler
  // sensor should read no colder than the mixed flow, which should read no
  // colder than the floor return (within the tolerance). Warning-only.
  bool sensor_role_check_enabled;
  float sensor_role_tolerance_c;

  // --- Inferred DHW --------------------------------------------------------
  // Optional fallback when no DHW meter is configured. A boiler-temperature
  // drop while the heater is energised is treated as DHW charging. The
  // inference ends when the temperature recovers; an over-long charge raises a
  // warning so the assumption can be reviewed.
  bool dhw_infer_enabled;
  float dhw_infer_drop_c;
  uint32_t dhw_infer_window_s;
  float dhw_infer_recovery_c;
  uint32_t dhw_infer_max_s;

  // --- Output driver / port expander --------------------------------------
  // The v0.1 board drives its relays through a 74HC595-compatible serial
  // extender; the v1.x board uses a PCF8574. Both are runtime-selectable so
  // the same firmware can drive either board (and a custom I2C address)
  // without a rebuild.
  uint8_t output_expander;       // OUTPUT_EXPANDER_* enum
  uint8_t expander_address;      // 7-bit I2C address for PCF8574 (e.g. 0x20)
  uint8_t expander_data_pin;     // 74HC595 SER / DS
  uint8_t expander_clock_pin;    // 74HC595 SRCLK / SHCP
  uint8_t expander_latch_pin;    // 74HC595 RCLK / STCP
  uint8_t expander_oe_pin;       // 74HC595 /OE, 255 when hard-wired
  bool expander_oe_active_low;   // true: /OE low enables the outputs
  // Legacy global polarity, kept only so v3 NVS blobs can be migrated. New
  // code uses the per-channel flags below; the portal no longer edits this.
  bool output_active_low;
  // Expander output bit (0-7) for each logical channel. Direction and motion
  // are the two mixer channels described by MixerWiring. 0xFF = not connected.
  uint8_t expander_bit_heater;
  uint8_t expander_bit_pump_rad1;
  uint8_t expander_bit_pump_rad2;
  uint8_t expander_bit_pump_floor;
  uint8_t expander_bit_mix_direction;
  uint8_t expander_bit_mix_motion;
  uint8_t expander_bit_mux_s0;
  uint8_t expander_bit_mux_s1;

  // --- Per-channel output polarity ----------------------------------------
  // Each logical output has its own polarity because one board may mix
  // active-high and active-low relay drivers. A logical ON drives the output
  // low when the corresponding flag is true.
  bool output_active_low_heater;
  bool output_active_low_pump_rad1;
  bool output_active_low_pump_rad2;
  bool output_active_low_pump_floor;
  bool output_active_low_mix_direction;
  bool output_active_low_mix_motion;

  // --- Port-expander fault input ------------------------------------------
  // PCF8574 P7 on HomeNet Heating control v1.x is an input from the relay
  // board's software interlock. It asserts when the open and close relays are
  // energised together. 0xFF disables monitoring.
  uint8_t expander_bit_mix_fault;
  bool mix_fault_active_low;

  // --- Current measurement -------------------------------------------------
  // v0.1: four current sensors through a 74HC4051 into one ADC pin.
  // v1.x: ADS1115 A0/A1/A3/A4. The TLV9064 sensor assembly is external and
  // optional; when it is absent the firmware must not use current protection.
  uint8_t current_sense;        // CURRENT_SENSE_* enum
  uint8_t ads1115_address;      // 7-bit I2C address (default 0x48)
  bool tlv9064_present;
};

inline bool current_sense_available(const Config& config) {
  if (config.current_sense == CURRENT_SENSE_NONE) {
    return false;
  }
  if (config.current_sense == CURRENT_SENSE_ADS1115 && !config.tlv9064_present) {
    return false;
  }
  return true;
}

// Mixer opening ceiling as a fraction (0.01-1.0). Single source of truth for
// the L1 enforcement and the higher-layer clamps.
inline float mixer_ceiling(const Config& config) {
  uint8_t pct = config.mix_max_open_pct;
  if (pct < 1) pct = 1;
  if (pct > 100) pct = 100;
  return static_cast<float>(pct) / 100.0f;
}

// Normal floor-demand minimum opening as a fraction (0-1). Zero means the
// valve may close fully during normal control; safety closes always use 0.
inline float mixer_minimum(const Config& config) {
  uint8_t pct = config.mix_min_open_pct;
  if (pct > 100) pct = 100;
  return static_cast<float>(pct) / 100.0f;
}

// Effective ceiling for the current boiler-side temperature side. `above` is
// the hysteresis state maintained by L1. The absolute mix_max_open_pct always
// wins when it is lower.
inline float mixer_effective_ceiling(const Config& config, bool above) {
  const float absolute = mixer_ceiling(config);
  if (!config.mix_max_dynamic) {
    return absolute;
  }
  uint8_t pct = above ? config.mix_max_above_pct : config.mix_max_below_pct;
  if (pct < 1) pct = 1;
  if (pct > 100) pct = 100;
  const float dynamic = static_cast<float>(pct) / 100.0f;
  return dynamic < absolute ? dynamic : absolute;
}

// Compiled-in defaults used before NVS has valid data. All values are safe
// placeholders; safety-relevant numbers must be finalized in the plan and
// bounded by L1/L3 validation.
Config config_defaults();

// The single active configuration instance. Tasks hold a const reference to
// this object, so a settings write becomes visible on the next tick. Writes
// must go through the web portal's validated section handlers.
Config& config_mutable();
const Config& config_active();
