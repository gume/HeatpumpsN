#include "hpn/l2_control.h"

#include <Arduino.h>
#include <math.h>

#include "hpn/web_portal.h"

namespace hpn {
namespace l2 {
namespace {

constexpr float kIntegratorLimit = 0.35f;
constexpr uint32_t kDeltaTCollapseDebounceMs = 60000u;

uint32_t g_last_tick_ms = 0;
uint32_t g_floor_flush_end_ms = 0;
uint32_t g_floor_pause_end_ms = 0;
uint32_t g_delta_low_since_ms = 0;
uint32_t g_long_pause_since_ms = 0;
bool g_floor_flush_active = false;
bool g_floor_pause_active = false;
bool g_floor_was_on = false;
float g_integrator = 0.0f;

float clampf(float value, float low, float high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

bool reading_valid(const SensorReading& reading) {
  return reading.is_valid && isfinite(reading.value_c);
}

float reading_or(const SensorReading& reading, float fallback) {
  return reading_valid(reading) ? reading.value_c : fallback;
}

bool radiator_active(const SystemState& state) {
  return state.pump_rad1_requested || state.pump_rad2_requested || state.pump_rad1_phys ||
         state.pump_rad2_phys;
}

// Simple proportional + feedforward mixer loop around the dead-reckoned
// position. The Smith predictor term uses the measured gradient and the
// configured transport delay. Full PI coordination and observer-owned tuning
// validation remain future work.
void service_mixer_control(const Config& config, SystemState& state, bool floor_demand,
                           bool dhw_active, bool flush_active) {
  // Emergency lock: L1 owns the mixer and forces a full close. Do not let the
  // closed loop overwrite the target while the lock is held.
  if (state.mix_emergency) {
    state.mix_target_pos = 0.0f;
    state.integrator_val = g_integrator;
    return;
  }

  if (!floor_demand) {
    if (state.heater_requested || state.pump_rad1_requested || state.pump_rad2_requested) {
      state.mix_target_pos = 0.0f;
    }
    state.integrator_val = g_integrator;
    return;
  }

  const float setpoint = config.floor_setpoint_shadow;
  if (dhw_active) {
    // Freeze the integrator and hold/close safely while the boiler diverts
    // heat to domestic hot water.
    const float boiler = reading_or(state.T_boiler, setpoint + config.deadband_temp);
    if (boiler < setpoint + config.deadband_temp) {
      state.mix_target_pos = 0.0f;
    }
    state.integrator_val = g_integrator;
    return;
  }

  const float boiler = reading_or(state.T_boiler, setpoint + 10.0f);
  const float ret = reading_or(state.T_return_floor, setpoint - 5.0f);
  const float measured = reading_or(state.T_mix_out, setpoint);
  const float gradient = reading_valid(state.T_mix_out) ? state.T_mix_out.gradient_k_per_s : 0.0f;
  const float rad = radiator_active(state) ? 1.0f : 0.0f;

  const float hot_span = config.ff_boiler * boiler - config.ff_return * ret;
  float pos_ff = 0.0f;
  if (fabsf(hot_span) > 1.0f) {
    pos_ff = (setpoint - config.mix_ff_offset + config.rad_coupling_ff * rad -
              config.ff_return * ret) /
             hot_span;
  }
  pos_ff = clampf(pos_ff, 0.0f, 1.0f);

  const float predicted = measured + gradient * config.delay_s;
  const float error = setpoint - predicted;
  const bool supply_ok = boiler >= setpoint + config.deadband_temp;
  const bool gated = !supply_ok || !state.heater_requested || flush_active;

  if (gated) {
    g_integrator *= 0.98f;
  } else if (fabsf(error) > config.deadband_temp) {
    g_integrator += config.ki * error;
  }
  g_integrator = clampf(g_integrator, -kIntegratorLimit, kIntegratorLimit);
  state.integrator_val = g_integrator;

  // If the measured temperature is already at target, hold the current valve
  // position. The feedforward model is not accurate enough to justify moving
  // to an absolute position when the feedback error is inside the deadband.
  // During the warm-up flush the position is still clamped (below), so the
  // hold is skipped there.
  if (!flush_active && fabsf(error) <= config.deadband_temp) {
    state.mix_target_pos = state.pos_est_dr;
    return;
  }

  float target = pos_ff + config.kp * error + g_integrator;
  if (flush_active) {
    target = fminf(target, config.flush_max_pos);
  }

  // Limit how far the target can move in one control tick. This prevents large
  // feedforward corrections from producing long motor bursts.
  const float max_step = 0.10f;
  if (target > state.pos_est_dr + max_step) {
    target = state.pos_est_dr + max_step;
  } else if (target < state.pos_est_dr - max_step) {
    target = state.pos_est_dr - max_step;
  }
  state.mix_target_pos = clampf(target, 0.0f, 1.0f);
}

void service_position_tracking(const Config& config, SystemState& state, uint32_t now_ms) {
  if (!state.pos_encoder_valid) {
    return;
  }
  const uint32_t staleness_ms = config.encoder_staleness_s * 1000u;
  if (staleness_ms > 0 && now_ms - state.pos_encoder_ms > staleness_ms) {
    state.pos_encoder_valid = false;
    state.active_alarms |= WARN_ENCODER_STALE;
    return;
  }
  state.active_alarms &= ~WARN_ENCODER_STALE;
  if (fabsf(state.pos_est_dr - state.pos_encoder) > config.recalc_threshold) {
    state.pos_est_dr = state.pos_encoder;
    state.active_alarms |= WARN_ENCODER_DIVERGED;
    web::log_event(0, 1201, "Mixer position resynced from MQTT meter");
  } else {
    state.active_alarms &= ~WARN_ENCODER_DIVERGED;
  }
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
  g_last_tick_ms = 0;
  g_floor_flush_end_ms = 0;
  g_floor_pause_end_ms = 0;
  g_delta_low_since_ms = 0;
  g_long_pause_since_ms = 0;
  g_floor_flush_active = false;
  g_floor_pause_active = false;
  g_floor_was_on = false;
  g_integrator = 0.0f;
}

void tick(const Config& config, SystemState& state) {
  const uint32_t now = millis();
  if (g_last_tick_ms == 0) {
    g_last_tick_ms = now;
  }
  const uint32_t dt_ms = now - g_last_tick_ms;
  g_last_tick_ms = now;
  const uint32_t dt_s = (dt_ms + 500u) / 1000u;

  // Test Mode bypasses the coordination matrix and passes intents straight
  // through. L1 still arbitrates safety and drives the relays.
  if (state.mode == MODE_TEST_OVERRIDE) {
    state.heater_requested = state.heater_intended;
    state.pump_floor_requested = state.pump_floor_intended;
    state.pump_rad1_requested = state.pump_rad1_intended;
    state.pump_rad2_requested = state.pump_rad2_intended;
    service_position_tracking(config, state, now);
    g_floor_was_on = state.pump_floor_requested;
    return;
  }

  // --- Command keep-alive and after-heating timers ------------------------
  if (state.heater_intended) {
    if (state.keepalive_age_s < 0xFFFFFFFFu - dt_s) {
      state.keepalive_age_s += dt_s;
    }
  } else {
    state.keepalive_age_s = 0;
  }

  const bool pumps_intended = state.pump_floor_intended || state.pump_rad1_intended ||
                              state.pump_rad2_intended;
  if (!state.heater_requested && pumps_intended) {
    if (state.after_heating_timer_s < 0xFFFFFFFFu - dt_s) {
      state.after_heating_timer_s += dt_s;
    }
  } else {
    state.after_heating_timer_s = 0;
  }

  // --- Start from HA intents ----------------------------------------------
  bool req_heater = state.heater_intended;
  bool req_floor = state.pump_floor_intended;
  bool req_rad1 = state.pump_rad1_intended;
  bool req_rad2 = state.pump_rad2_intended;
  DivergenceReason div = DIV_NONE;

  // --- Summer / anti-seize lockout ----------------------------------------
  if (state.mode == MODE_SUMMER || state.mode == MODE_ANTI_SEIZE) {
    req_heater = false;
    req_floor = false;
    req_rad1 = false;
    req_rad2 = false;
  }

  // --- Keep-alive timeout --------------------------------------------------
  if (state.heater_intended && state.keepalive_age_s >= config.demand_timeout_s) {
    req_heater = false;
    div = DIV_DEMAND_LOSS;
    if (state.system_error_code == ERR_OK) {
      state.system_error_code = ERR_HA_KEEPALIVE_TIMEOUT;
    }
  }

  // --- After-heating pump shutdown ----------------------------------------
  if (!req_heater && pumps_intended &&
      state.after_heating_timer_s >= config.pump_stop_after_heating_s) {
    req_floor = false;
    req_rad1 = false;
    req_rad2 = false;
    div = DIV_AFTER_HEATING_TIMEOUT;
    if (state.system_error_code == ERR_OK) {
      state.system_error_code = ERR_AFTER_HEAT_TIMEOUT;
    }
  }

  // --- Floor warm-up flush -------------------------------------------------
  if (req_floor && !g_floor_was_on) {
    g_floor_flush_active = true;
    g_floor_flush_end_ms = now + config.start_flush_ms;
    state.flush_state = FLUSH_ACTIVE;
    state.t_ret_flush_valid = false;
  } else if (!req_floor) {
    g_floor_flush_active = false;
    if (!g_floor_pause_active) {
      state.flush_state = FLUSH_IDLE;
    }
    state.t_ret_flush_valid = true;
  }
  if (g_floor_flush_active && static_cast<int32_t>(now - g_floor_flush_end_ms) >= 0) {
    g_floor_flush_active = false;
    state.flush_state = FLUSH_IDLE;
    state.t_ret_flush_valid = true;
  }

  // --- Floor delta-T collapse ---------------------------------------------
  if (!g_floor_pause_active && req_floor && !g_floor_flush_active && state.t_ret_flush_valid &&
      !state.dhw_active && reading_valid(state.T_mix_out) &&
      reading_valid(state.T_return_floor)) {
    const float delta_t = state.T_mix_out.value_c - state.T_return_floor.value_c;
    if (delta_t < config.min_useful_delta_t) {
      if (g_delta_low_since_ms == 0) {
        g_delta_low_since_ms = now;
      } else if (now - g_delta_low_since_ms >= kDeltaTCollapseDebounceMs) {
        req_floor = false;
        state.mix_target_pos = 0.0f;
        state.flush_state = FLUSH_PAUSED_DELTA_T;
        div = DIV_FLOOR_DELTA_T_COLLAPSED;
        g_floor_pause_active = true;
        g_floor_pause_end_ms = now + config.floor_pause_duration_s * 1000u;
        g_delta_low_since_ms = 0;
        if (!req_rad1 && !req_rad2) {
          req_heater = false;
        }
      }
    } else {
      g_delta_low_since_ms = 0;
    }
  } else if (!req_floor) {
    g_delta_low_since_ms = 0;
  }

  if (g_floor_pause_active && static_cast<int32_t>(now - g_floor_pause_end_ms) >= 0) {
    g_floor_pause_active = false;
    req_floor = state.pump_floor_intended;
    state.flush_state = FLUSH_IDLE;
  }

  // --- Long pause with heater on and no pumps ------------------------------
  if (req_heater && !req_floor && !req_rad1 && !req_rad2) {
    if (g_long_pause_since_ms == 0) {
      g_long_pause_since_ms = now;
    } else if (now - g_long_pause_since_ms >= config.long_pause_s * 1000u) {
      req_heater = false;
      div = DIV_LONG_PAUSE;
    }
  } else {
    g_long_pause_since_ms = 0;
  }

  // --- DHW priority -------------------

  // The boiler relay stays as requested; the mixer and integrator are handled
  // by service_mixer_control(). Reflect the priority in the divergence field.
  if (state.dhw_active) {
    div = DIV_DHW_PRIORITY_ACTIVE;
  }

  // --- Apply coordination requests ----------------------------------------
  state.heater_requested = req_heater;
  state.pump_floor_requested = req_floor;
  state.pump_rad1_requested = req_rad1;
  state.pump_rad2_requested = req_rad2;

  // --- Mixer position loop ------------------------------------------------
  service_mixer_control(config, state, req_floor, state.dhw_active, g_floor_flush_active);
  service_position_tracking(config, state, now);

  const bool safety_error = state.system_error_code == ERR_L1_HARD_OVERTEMP ||
                            state.system_error_code == ERR_SENSOR_MIX_LOST ||
                            state.system_error_code == ERR_SENSOR_RET_LOST ||
                            state.system_error_code == ERR_SENSOR_BOILER_LOST ||
                            state.system_error_code == ERR_RELAY_FAULT_WELDED;
  if (!safety_error) {
    state.divergence_reason = div;
  }

  g_floor_was_on = req_floor;

  if (!state.heater_intended && state.system_error_code == ERR_HA_KEEPALIVE_TIMEOUT) {
    state.system_error_code = ERR_OK;
  }
  if (req_floor && state.system_error_code == ERR_AFTER_HEAT_TIMEOUT) {
    state.system_error_code = ERR_OK;
  }
}

}  // namespace l2
}  // namespace hpn
