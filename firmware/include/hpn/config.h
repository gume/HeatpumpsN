#pragma once

#include <stdint.h>

// Sentinel for "GPIO not assigned yet". Plans 02 still marks most pins TBD.
#define HPN_GPIO_UNASSIGNED 0xFF

// Mixer actuator wiring. The two logical channels are always open/direction
// and close/motion; this setting tells L0 how the carrier exposes them.
enum MixerWiring : uint8_t {
  MIX_WIRING_DIRECTION_MOTION = 0,  // legacy: direction + motion enable
  MIX_WIRING_OPEN_CLOSE = 1,        // new: separate open and close relays
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
  // Deprecated and unused: the mixer is now a continuous target-position servo
  // (L1 stops at `mix_target_pos`), so there is no pulse length or cooldown.
  // These three are retained only so saved-NVS blobs keep their binary layout;
  // they are not exposed in the web configuration and never read by control.
  uint32_t min_pulse_ms;
  uint32_t max_pulse_ms;
  uint32_t pulse_cooldown_ms;
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

  // =========================================================================
  // APPEND-ONLY FIELDS
  //
  // Keep new fields at the end so config_store can migrate older NVS blobs by
  // copying the known prefix and filling the remainder with defaults.
  // =========================================================================

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
};

// Compiled-in defaults used before NVS has valid data. All values are safe
// placeholders; safety-relevant numbers must be finalized in the plan and
// bounded by L1/L3 validation.
Config config_defaults();

// The single active configuration instance. Tasks hold a const reference to
// this object, so a settings write becomes visible on the next tick. Writes
// must go through the web portal's validated section handlers.
Config& config_mutable();
const Config& config_active();
