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
bool g_overshoot_latched = false;

// Inferred-DHW state. The peak is the most recent boiler-side temperature
// before the drop that starts an inference.
bool g_dhw_inferred = false;
bool g_dhw_timeout = false;
uint32_t g_dhw_infer_start_ms = 0;
uint32_t g_dhw_peak_ms = 0;
float g_dhw_peak_c = NAN;

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

float mixer_ceiling_or_state(const Config& config, const SystemState& state) {
  const float ceiling =
      isfinite(state.mix_ceiling) && state.mix_ceiling > 0.0f ? state.mix_ceiling
                                                             : mixer_ceiling(config);
  return clampf(ceiling, 0.01f, 1.0f);
}

// Soft overshoot reaction. This is separate from the L1 hard over-temperature
// rule: it is a control response with a warning, not a safety latch.
void service_overshoot(const Config& config, SystemState& state) {
  const float setpoint = config.floor_setpoint_shadow;
  const bool valid = reading_valid(state.T_mix_out);
  const float measured = valid ? state.T_mix_out.value_c : NAN;

  if (!g_overshoot_latched) {
    if (valid && isfinite(setpoint) && measured >= setpoint + config.mix_overshoot_delta_c) {
      g_overshoot_latched = true;
      state.mix_overshoot = true;
      state.active_alarms |= WARN_MIX_OVERSHOOT;
      web::log_event(1, 1310, "Mixer forward temperature above setpoint + delta; reducing opening");
    }
  } else if (valid && isfinite(setpoint) &&
             measured <= setpoint - config.mix_overshoot_recovery_c) {
    g_overshoot_latched = false;
    state.mix_overshoot = false;
    state.active_alarms &= ~WARN_MIX_OVERSHOOT;
    web::log_event(0, 1311, "Mixer forward temperature recovered; normal control resumed");
  }

  if (!valid) {
    // A lost sensor is handled by L1; do not keep a stale reaction latched.
    g_overshoot_latched = false;
    state.mix_overshoot = false;
    state.active_alarms &= ~WARN_MIX_OVERSHOOT;
  }
}

// Optional inferred DHW. A drop of the boiler-side temperature while the
// heater is physically on is treated as DHW charging. It ends on recovery or,
// after the configured maximum, with a warning so the assumption can be
// reviewed. The meter signal, when configured, always remains authoritative.
void service_dhw_inference(const Config& config, SystemState& state, uint32_t now) {
  if (!config.dhw_infer_enabled) {
    g_dhw_inferred = false;
    g_dhw_timeout = false;
    g_dhw_peak_c = NAN;
    g_dhw_peak_ms = 0;
    state.dhw_inferred = false;
    state.active_alarms &= ~WARN_DHW_INFER_TIMEOUT;
    if (state.system_error_code == ERR_DHW_INFER_TIMEOUT) state.system_error_code = ERR_OK;
    return;
  }

  const bool boiler_valid = reading_valid(state.T_boiler);
  const bool heater_on = state.heater_phys;
  if (!heater_on || !boiler_valid) {
    g_dhw_inferred = false;
    state.dhw_inferred = false;
    g_dhw_peak_c = NAN;
    g_dhw_peak_ms = 0;
    g_dhw_timeout = false;
    state.active_alarms &= ~WARN_DHW_INFER_TIMEOUT;
    if (state.system_error_code == ERR_DHW_INFER_TIMEOUT) state.system_error_code = ERR_OK;
    return;
  }

  const float temp = state.T_boiler.value_c;
  if (!g_dhw_inferred) {
    if (g_dhw_timeout) {
      // A timeout must not restart the inference on the same temperature drop,
      // otherwise the warning would clear and re-latch every cycle. Hold it
      // until the boiler recovers or the heater turns off (which resets the
      // whole inference above).
      const bool recovered =
          isfinite(g_dhw_peak_c) && temp >= g_dhw_peak_c - config.dhw_infer_recovery_c;
      if (!recovered) {
        return;
      }
      g_dhw_timeout = false;
      state.active_alarms &= ~WARN_DHW_INFER_TIMEOUT;
      if (state.system_error_code == ERR_DHW_INFER_TIMEOUT) state.system_error_code = ERR_OK;
      g_dhw_peak_c = temp;
      g_dhw_peak_ms = now;
      web::log_event(0, 1323, "Inferred DHW timeout cleared after boiler recovery");
      return;
    }

    const uint32_t window_ms = config.dhw_infer_window_s * 1000u;
    if (!isfinite(g_dhw_peak_c) || (window_ms > 0 && now - g_dhw_peak_ms > window_ms)) {
      g_dhw_peak_c = temp;
      g_dhw_peak_ms = now;
    } else if (temp > g_dhw_peak_c) {
      g_dhw_peak_c = temp;
      g_dhw_peak_ms = now;
    }

    if (isfinite(g_dhw_peak_c) && temp <= g_dhw_peak_c - config.dhw_infer_drop_c) {
      g_dhw_inferred = true;
      g_dhw_timeout = false;
      g_dhw_infer_start_ms = now;
      state.dhw_inferred = true;
      state.active_alarms &= ~WARN_DHW_INFER_TIMEOUT;
      if (state.system_error_code == ERR_DHW_INFER_TIMEOUT) state.system_error_code = ERR_OK;
      web::log_event(0, 1320, "Inferred DHW charging start (boiler temperature drop)");
    }
    return;
  }

  const bool recovered = temp >= g_dhw_peak_c - config.dhw_infer_recovery_c;
  if (recovered) {
    g_dhw_inferred = false;
    state.dhw_inferred = false;
    g_dhw_timeout = false;
    g_dhw_peak_c = temp;
    g_dhw_peak_ms = now;
    state.active_alarms &= ~WARN_DHW_INFER_TIMEOUT;
    if (state.system_error_code == ERR_DHW_INFER_TIMEOUT) state.system_error_code = ERR_OK;
    web::log_event(0, 1321, "Inferred DHW charging ended (boiler temperature recovered)");
    return;
  }

  if (!g_dhw_timeout && config.dhw_infer_max_s > 0 &&
      now - g_dhw_infer_start_ms >= config.dhw_infer_max_s * 1000u) {
    g_dhw_timeout = true;
    // Stop blocking the floor indefinitely, but keep the warning latched until
    // a recovery or a new heater cycle confirms the assumption.
    g_dhw_inferred = false;
    state.dhw_inferred = false;
    state.active_alarms |= WARN_DHW_INFER_TIMEOUT;
    if (state.system_error_code == ERR_OK) {
      state.system_error_code = ERR_DHW_INFER_TIMEOUT;
    }
    web::log_event(2, 1322, "Inferred DHW charging exceeded the configured maximum");
  }
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

  const float ceiling = mixer_ceiling_or_state(config, state);
  const float minimum = fminf(mixer_minimum(config), ceiling);
  // Never report or chase a target above the effective opening limit.
  if (state.mix_target_pos > ceiling) {
    state.mix_target_pos = ceiling;
  }

  if (!floor_demand) {
    // No floor demand: close fully regardless of what other circuits do. The
    // minimum opening is a normal-control limit only and never blocks a close.
    state.mix_target_pos = 0.0f;
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

  if (state.mix_overshoot) {
    // Soft setpoint + delta reaction: drive to the configured reduced opening
    // and hold until L2 observes recovery. This intentionally bypasses the
    // normal minimum so a zero-percent reaction is possible.
    uint8_t target_pct = config.mix_overshoot_target_pct;
    if (target_pct > 100) target_pct = 100;
    float reaction = static_cast<float>(target_pct) / 100.0f;
    if (reaction > ceiling) reaction = ceiling;
    state.mix_target_pos = reaction;
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
    float hold = fminf(state.pos_est_dr, ceiling);
    if (hold < minimum) hold = minimum;
    state.mix_target_pos = hold;
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

  // Minimum opening applies only to normal floor-demand control. The flush
  // cap, DHW handling and every close path above are deliberately excluded.
  if (!flush_active && target < minimum) {
    target = minimum;
  }
  state.mix_target_pos = clampf(target, 0.0f, ceiling);
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
  g_overshoot_latched = false;
  g_dhw_inferred = false;
  g_dhw_timeout = false;
  g_dhw_infer_start_ms = 0;
  g_dhw_peak_ms = 0;
  g_dhw_peak_c = NAN;
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

  // --- Optional inferred DHW and combined priority ------------------------
  service_dhw_inference(config, state, now);
  state.dhw_active = state.dhw_meter_active || state.dhw_inferred;
  service_overshoot(config, state);

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
