#include "hpn/l1_safety.h"

#include <Arduino.h>
#include <math.h>

#include "hpn/l0_io.h"
#include "hpn/web_portal.h"

namespace hpn {
namespace l1 {
namespace {

uint32_t g_mixer_end_ms = 0;
uint32_t g_mixer_last_ms = 0;
uint32_t g_heater_off_ms = 0;
bool g_overtemp_latched = false;
uint8_t g_dry_run_count[3] = {};
uint8_t g_welded_count[3] = {};

void apply_relay(Relay relay, bool requested, bool& physical, const char* name) {
  if (requested == physical) {
    return;
  }
  l0::set_relay(relay, requested);
  physical = requested;
  Serial.printf("[l1] %s -> %s\n", name, requested ? "ON" : "OFF");
}

bool persistent_fault(const SensorReading& reading, uint8_t threshold) {
  return !reading.is_valid && reading.consecutive_errors >= threshold;
}

// Execute at most one mixer pulse at a time and dead-reckon position from the
// arbitrated direction. Reaching 0 % or 100 % is a soft end stop and re-zeroes
// the estimate; a free-run request (homing/sweep) ignores the soft limits.
void service_mixer(const Config& config, SystemState& state, int8_t dir_req, uint32_t ms_req) {
  const uint32_t now = millis();
  if (g_mixer_last_ms == 0) {
    g_mixer_last_ms = now;
  }

  if (state.mix_pulse_ms_phys > 0) {
    const uint32_t dt = now - g_mixer_last_ms;
    g_mixer_last_ms = now;

    const uint32_t travel_ms = config.travel_time_ms > 0 ? config.travel_time_ms : 145000;
    const float step = static_cast<float>(dt) / static_cast<float>(travel_ms);
    float next = state.pos_est_dr + (state.mix_pulse_dir_phys > 0 ? step : -step);

    bool at_limit = false;
    if (!state.mix_free_run) {
      if (next <= 0.0f) {
        next = 0.0f;
        at_limit = true;
      } else if (next >= 1.0f) {
        next = 1.0f;
        at_limit = true;
      }
    }
    state.pos_est_dr = next;

    if (at_limit || static_cast<int32_t>(now - g_mixer_end_ms) >= 0) {
      l0::set_mixer_motor(false, false);
      state.mix_pulse_ms_phys = 0;
      state.mix_pulse_dir_phys = 0;
    }
    return;
  }

  g_mixer_last_ms = now;
  if (ms_req == 0 || dir_req == 0) {
    return;
  }

  state.mix_pulse_dir_phys = dir_req > 0 ? 1 : -1;
  state.mix_pulse_ms_phys = ms_req;
  g_mixer_end_ms = now + ms_req;
  g_mixer_last_ms = now;
  l0::set_mixer_motor(true, state.mix_pulse_dir_phys > 0);
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
  g_mixer_end_ms = 0;
  g_mixer_last_ms = 0;
  g_heater_off_ms = millis();  // hold anti-short-cycle at cold boot
  g_overtemp_latched = false;
  for (uint8_t i = 0; i < 3; ++i) {
    g_dry_run_count[i] = 0;
    g_welded_count[i] = 0;
  }
}

void tick(const Config& config, SystemState& state) {
  const uint32_t now = millis();

  // --- Start from L2 requests ---------------------------------------------
  bool heater_req = state.heater_requested;
  bool floor_req = state.pump_floor_requested;
  bool rad1_req = state.pump_rad1_requested;
  bool rad2_req = state.pump_rad2_requested;

  // --- Sensor posture ------------------------------------------------------
  const bool mix_fault = persistent_fault(state.T_mix_out, config.sensor_error_threshold);
  const bool ret_fault = persistent_fault(state.T_return_floor, config.sensor_error_threshold);
  const bool boiler_fault = persistent_fault(state.T_boiler, config.sensor_error_threshold);
  bool force_mixer_close = false;
  bool allow_mixer_open = true;

  if (mix_fault) {
    state.active_alarms |= ALARM_SENSOR_FAULT_MIX;
    state.system_error_code = ERR_SENSOR_MIX_LOST;
    state.divergence_reason = DIV_OVERTEMP_PAUSE;
    force_mixer_close = true;
    floor_req = false;
  } else {
    state.active_alarms &= ~ALARM_SENSOR_FAULT_MIX;
    if (state.system_error_code == ERR_SENSOR_MIX_LOST) state.system_error_code = ERR_OK;
  }

  if (ret_fault) {
    state.active_alarms |= WARN_SENSOR_FAULT_RET;
    if (state.system_error_code == ERR_OK) state.system_error_code = ERR_SENSOR_RET_LOST;
  } else {
    state.active_alarms &= ~WARN_SENSOR_FAULT_RET;
    if (state.system_error_code == ERR_SENSOR_RET_LOST) state.system_error_code = ERR_OK;
  }

  if (boiler_fault) {
    state.active_alarms |= WARN_SENSOR_FAULT_BOILER;
    if (state.system_error_code == ERR_OK) state.system_error_code = ERR_SENSOR_BOILER_LOST;
    // Conservative cap: do not run the floor mixer wide open without a
    // trustworthy hot-side temperature.
    if (state.mix_target_pos > 0.35f) state.mix_target_pos = 0.35f;
    allow_mixer_open = false;
  } else {
    state.active_alarms &= ~WARN_SENSOR_FAULT_BOILER;
    if (state.system_error_code == ERR_SENSOR_BOILER_LOST) state.system_error_code = ERR_OK;
  }

  // --- Hard floor over-temperature ----------------------------------------
  const bool mix_valid = state.T_mix_out.is_valid && isfinite(state.T_mix_out.value_c);
  if (mix_valid && state.T_mix_out.value_c >= config.t_max_hard) {
    g_overtemp_latched = true;
    state.active_alarms |= ALARM_OVERTEMP_HARD;
    state.divergence_reason = DIV_OVERTEMP_PAUSE;
    state.system_error_code = ERR_L1_HARD_OVERTEMP;
  }
  if (g_overtemp_latched && mix_valid &&
      state.T_mix_out.value_c < config.t_max_warn - config.t_overtemp_hysteresis) {
    g_overtemp_latched = false;
  }
  if (!g_overtemp_latched) {
    state.active_alarms &= ~ALARM_OVERTEMP_HARD;
    if (state.system_error_code == ERR_L1_HARD_OVERTEMP) state.system_error_code = ERR_OK;
  } else {
    floor_req = false;
    force_mixer_close = true;
  }

  // --- High-temperature warning band --------------------------------------
  if (mix_valid && state.T_mix_out.value_c >= config.t_max_warn) {
    state.active_alarms |= WARN_OVERTEMP_HIGH;
    allow_mixer_open = false;
  } else {
    state.active_alarms &= ~WARN_OVERTEMP_HIGH;
  }

  // --- Gradient cutoff -----------------------------------------------------
  if (mix_valid && state.T_mix_out.gradient_k_per_s > config.grad_limit) {
    state.active_alarms |= WARN_GRADIENT_CUTOFF;
    allow_mixer_open = false;
  } else {
    state.active_alarms &= ~WARN_GRADIENT_CUTOFF;
  }

  // --- Summer / anti-seize heater lockout ---------------------------------
  if (state.mode == MODE_SUMMER || state.mode == MODE_ANTI_SEIZE) {
    heater_req = false;
  }

  // --- Anti-short-cycle backstop ------------------------------------------
  if (heater_req && !state.heater_phys && config.boiler_min_off_ms > 0 &&
      now - g_heater_off_ms < config.boiler_min_off_ms) {
    heater_req = false;
    state.divergence_reason = DIV_ANTI_SHORT_CYCLE;
  }
  if (!heater_req && state.heater_phys) {
    g_heater_off_ms = now;
  }

  // --- Keep-alive backstop -------------------------------------------------
  if (heater_req && state.keepalive_age_s >= config.demand_timeout_s) {
    heater_req = false;
    state.divergence_reason = DIV_DEMAND_LOSS;
    state.system_error_code = ERR_HA_KEEPALIVE_TIMEOUT;
  }

  // --- After-heating pump backstop ----------------------------------------
  if (!heater_req && state.after_heating_timer_s >= config.pump_stop_after_heating_s &&
      (floor_req || rad1_req || rad2_req)) {
    floor_req = false;
    rad1_req = false;
    rad2_req = false;
    state.divergence_reason = DIV_AFTER_HEATING_TIMEOUT;
    state.system_error_code = ERR_AFTER_HEAT_TIMEOUT;
  }

  // --- Relay arbitration ---------------------------------------------------
  const bool any_pump = floor_req || rad1_req || rad2_req;
  const bool heater_on = heater_req && any_pump;
  if (heater_req && !any_pump) {
    state.active_alarms |= WARN_HEATER_NO_PUMP;
  } else {
    state.active_alarms &= ~WARN_HEATER_NO_PUMP;
  }

  apply_relay(Relay::PumpFloor, floor_req, state.pump_floor_phys, "pump_floor");
  apply_relay(Relay::PumpRad1, rad1_req, state.pump_rad1_phys, "pump_rad1");
  apply_relay(Relay::PumpRad2, rad2_req, state.pump_rad2_phys, "pump_rad2");
  apply_relay(Relay::Heater, heater_on, state.heater_phys, "heater");

  // --- Pump power plausibility (dry-run and welded relay) -----------------
  const bool pump_on[3] = {state.pump_floor_phys, state.pump_rad1_phys, state.pump_rad2_phys};
  bool dry_run = false;
  bool welded = false;
  for (uint8_t i = 0; i < 3; ++i) {
    if (!config.has_pump_power[i]) {
      g_dry_run_count[i] = 0;
      g_welded_count[i] = 0;
      continue;
    }
    const float watts = state.pump_power_w[i];
    if (pump_on[i] && watts < 2.0f) {
      if (g_dry_run_count[i] < 255) g_dry_run_count[i]++;
    } else {
      g_dry_run_count[i] = 0;
    }
    if (!pump_on[i] && watts > 2.0f) {
      if (g_welded_count[i] < 255) g_welded_count[i]++;
    } else {
      g_welded_count[i] = 0;
    }
    dry_run = dry_run || g_dry_run_count[i] >= 20;
    welded = welded || g_welded_count[i] >= 20;
  }
  if (dry_run) {
    state.active_alarms |= WARN_PUMP_DRY_RUN;
  } else {
    state.active_alarms &= ~WARN_PUMP_DRY_RUN;
  }
  if (welded) {
    state.active_alarms |= ALARM_WELDED_RELAY;
    state.system_error_code = ERR_RELAY_FAULT_WELDED;
    heater_req = false;
    floor_req = false;
    rad1_req = false;
    rad2_req = false;
    force_mixer_close = true;
  } else {
    state.active_alarms &= ~ALARM_WELDED_RELAY;
  }

  // --- Mixer pulse arbitration --------------------------------------------
  int8_t dir = state.mix_pulse_dir_req;
  uint32_t ms = state.mix_pulse_ms_req;
  if (force_mixer_close) {
    state.mix_target_pos = 0.0f;
    if (state.mix_pulse_ms_phys == 0 && state.mix_pulse_ms_req == 0) {
      dir = -1;
      ms = config.max_pulse_ms > 0 ? config.max_pulse_ms : 1000;
    } else {
      dir = 0;
      ms = 0;
    }
  } else if (dir > 0 && !allow_mixer_open) {
    dir = 0;
    ms = 0;
  }

  service_mixer(config, state, dir, ms);
  state.mix_pulse_dir_req = 0;
  state.mix_pulse_ms_req = 0;
}

}  // namespace l1
}  // namespace hpn
