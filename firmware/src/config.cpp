#include "hpn/config.h"

#include <stdio.h>

#include "hpn/board.h"
#include "hpn/state.h"

namespace {

template <size_t N>
void copy_str(char (&dst)[N], const char* src) {
  snprintf(dst, N, "%s", src);
}

}  // namespace

Config config_defaults() {
  Config c{};

  // --- Provisioning ---------------------------------------------------------
  copy_str(c.wifi_ssid, "");
  copy_str(c.wifi_password, "");
  copy_str(c.ap_ssid, "HeatpumpsN-Setup");
  copy_str(c.ap_password, "heatpumps-setup");
  copy_str(c.mqtt_host, "");
  c.mqtt_port = 1883;
  copy_str(c.mqtt_user, "");
  copy_str(c.mqtt_pass, "");
  copy_str(c.mqtt_base_topic, "heatpumpsn/controller");

  // --- Safety & Supervision Bounds -----------------------------------------
  c.t_max_hard = 50.0f;
  c.t_max_warn = 45.0f;
  c.t_overtemp_hysteresis = 2.0f;
  c.grad_limit = 0.5f;
  c.mix_safe_pos = 0.0f;
  c.boiler_min_off_ms = 180000;
  c.demand_timeout_s = 120;
  c.pump_stop_after_heating_s = 300;
  c.long_pause_s = 600;
  c.anti_seize_interval_s = 2592000;  // 30 days
  c.test_mode_max_s = 7200;
  c.sensor_error_threshold = 3;

  // --- Hardware & Actuator Constants ---------------------------------------
  c.setpoint_default = 30.0f;
  c.deadband_pos = 0.02f;
  c.deadband_temp = 0.2f;
  c.min_pulse_ms = 50;
  c.max_pulse_ms = 1000;
  c.pulse_cooldown_ms = 200;
  c.travel_time_ms = 120000;
  c.tick_ms = 2000;
  c.temp_poll_interval_ms = 1000;
  c.recalc_threshold = 0.10f;
  c.encoder_check_interval_s = 3600;
  c.encoder_check_pulses = 100;
  c.encoder_staleness_s = 300;
  c.pos_source = POS_SRC_HA_GENERIC;

  // --- I/O Assignment ------------------------------------------------------
  // Pins stay unassigned until the board layout is fixed (plan 02 §1).
  c.gpio_pump_floor = HPN_GPIO_UNASSIGNED;
  c.gpio_pump_rad1 = HPN_GPIO_UNASSIGNED;
  c.gpio_pump_rad2 = HPN_GPIO_UNASSIGNED;
  c.gpio_heater = HPN_GPIO_UNASSIGNED;
  c.gpio_mix_open = HPN_GPIO_UNASSIGNED;
  c.gpio_mix_close = HPN_GPIO_UNASSIGNED;
  c.gpio_onewire = HPN_GPIO_UNASSIGNED;
  c.gpio_led_data = HPN_GPIO_UNASSIGNED;
  c.num_leds = 5;
  c.has_pump_power[0] = false;
  c.has_pump_power[1] = false;
  c.has_pump_power[2] = false;
  c.i2c_sda = HPN_GPIO_UNASSIGNED;
  c.i2c_scl = HPN_GPIO_UNASSIGNED;
  copy_str(c.sensor_id_boiler, "");
  copy_str(c.sensor_id_ret, "");
  copy_str(c.sensor_id_mix, "");

  // --- Dynamic Heating Parameters (HA shadow) ------------------------------
  c.floor_setpoint_shadow = c.setpoint_default;
  c.summer_mode = false;

  // --- Control Loop Tuning (Observer / L3) ---------------------------------
  // Placeholders only. Values are to be derived from the analysis dataset
  // (analysis/) and validated by L3 before adoption.
  c.delay_s = 0.0f;
  c.gain = 1.0f;
  c.ff_boiler = 1.0f;
  c.ff_return = 0.0f;
  c.kp = 0.5f;
  c.ki = 0.05f;
  c.rad_coupling_ff = 0.0f;
  c.start_flush_ms = 30000;
  c.flush_max_pos = 0.20f;
  c.flush_delta_t_ok = 2.0f;
  c.min_useful_delta_t = 3.0f;
  c.floor_pause_duration_s = 300;

  // Board-specific wiring, sensor IDs and temporary hardware defaults.
  hpn::board_apply_defaults(c);

  return c;
}
