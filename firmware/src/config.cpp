#include "hpn/config.h"

#include <stdio.h>

#include "hpn/board.h"
#include "hpn/state.h"

namespace {

Config g_active_config{};

template <size_t N>
void copy_str(char (&dst)[N], const char* src) {
  snprintf(dst, N, "%s", src);
}

}  // namespace

Config& config_mutable() { return g_active_config; }
const Config& config_active() { return g_active_config; }

Config config_defaults() {
  Config c{};

  // --- Provisioning ---------------------------------------------------------
  // Temporary bench credentials. These belong in NVS once the Wi-Fi manager /
  // provisioning portal exists; Config is already the intended source.
  // Note: the bench AP SSID is case-sensitive ("Testing", not "testing").
  copy_str(c.wifi_ssid, "Testing");
  copy_str(c.wifi_password, "testing123");
  copy_str(c.ap_ssid, "HeatpumpsN-Setup");
  copy_str(c.ap_password, "heatpumps-setup");
  copy_str(c.mqtt_host, "");
  c.mqtt_port = 1883;
  copy_str(c.mqtt_user, "");
  copy_str(c.mqtt_pass, "");
  copy_str(c.mqtt_base_topic, "heatpumpsn/controller");
  copy_str(c.hostname, "heatpumpsn");
  c.ntp_enabled = true;
  copy_str(c.ntp_server, "pool.ntp.org");
  // POSIX TZ string for central Europe (CET/CEST). Editable in the portal.
  copy_str(c.timezone, "CET-1CEST,M3.5.0,M10.5.0/3");

  // --- Safety & Supervision Bounds -----------------------------------------
  c.t_max_hard = 50.0f;
  c.t_max_warn = 45.0f;
  c.t_overtemp_hysteresis = 2.0f;
  c.grad_limit = 0.3f;
  c.mix_safe_pos = 0.0f;
  c.mix_max_open_pct = 85;  // old controller ceiling; slow closing is unsafe
  c.boiler_min_off_ms = 180000;
  c.demand_timeout_s = 600;  // tolerate HA keep-alive every ~5 min
  c.pump_stop_after_heating_s = 300;
  c.long_pause_s = 600;
  c.anti_seize_interval_s = 2592000;  // 30 days
  c.test_mode_max_s = 7200;
  c.sensor_error_threshold = 3;

  // --- Hardware & Actuator Constants ---------------------------------------
  c.setpoint_default = 35.0f;
  c.deadband_pos = 0.02f;
  c.deadband_temp = 2.0f;
  c.travel_time_ms = 145000;
  c.tick_ms = 2000;
  c.temp_poll_interval_ms = 1000;
  c.recalc_threshold = 0.12f;
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
  c.mixer_wiring = MIX_WIRING_OPEN_CLOSE;

  copy_str(c.mixer_meter_device, "");
  copy_str(c.mixer_meter_field, "");
  c.mixer_meter_scale = 0;
  copy_str(c.mixer_meter_topic, "");
  copy_str(c.dhw_meter_device, "");
  copy_str(c.dhw_meter_entity, "");
  copy_str(c.dhw_meter_field, "dhw.charging");
  copy_str(c.dhw_meter_topic, "");
  c.mix_ff_offset = 6.75f;

  // --- MQTT log streaming --------------------------------------------------
  // Logs stay in RAM either way. Streaming is opt-in; empty topics mean the
  // base-derived defaults (<base>/log/slow and <base>/log/event).
  c.mqtt_log_enabled = false;
  copy_str(c.mqtt_log_slow_topic, "");
  copy_str(c.mqtt_log_event_topic, "");

  // --- Dynamic Heating Parameters (HA shadow) ------------------------------
  c.floor_setpoint_shadow = c.setpoint_default;
  c.summer_mode = false;

  // --- Control Loop Tuning (Observer / L3) ---------------------------------
  // Placeholders only. Values are to be derived from the analysis dataset
  // (analysis/) and validated by L3 before adoption.
  // Defaults derived from analysis/report.md §5d and §9. The mixer's "boiler"
  // sensor is the heat-exchanger secondary (T_incoming), so the corrected
  // model B feedforward gains are used.
  c.delay_s = 120.0f;
  c.gain = 1.0f;
  c.ff_boiler = 0.47f;
  c.ff_return = 0.87f;
  c.kp = 0.5f;
  c.ki = 0.02f;
  c.rad_coupling_ff = -1.5f;
  c.start_flush_ms = 30000;
  c.flush_max_pos = 0.20f;
  c.flush_delta_t_ok = 2.0f;
  c.min_useful_delta_t = 2.0f;
  c.floor_pause_duration_s = 900;

  // Board-specific wiring, sensor IDs and temporary hardware defaults.
  hpn::board_apply_defaults(c);

  return c;
}
