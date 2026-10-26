#include "hpn/l1_safety.h"

#include <Arduino.h>
#include <math.h>

#include "hpn/l0_io.h"
#include "hpn/web_portal.h"

namespace hpn {
namespace l1 {
namespace {

uint32_t g_mixer_last_ms = 0;
int8_t g_mix_last_dir = 0;  // last direction the relay was commanded to
uint32_t g_heater_off_ms = 0;
bool g_overtemp_latched = false;
uint8_t g_dry_run_count[3] = {};
uint8_t g_welded_count[3] = {};

// Minimum time the mixer motion stays de-energized when the direction changes,
// so the relay contacts have physically dropped before the opposite coil is
// driven. The stop / wait / switch-order start sequence lives here in L1
// because it owns the dead-reckoned position and the exact energised interval.
constexpr uint32_t kMixerDirSwitchMs = 50u;

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

// L1 mixer position servo. Every other layer (L2, the web portal, the test
// sequences and the emergency logic) only sets `mix_target_pos`; this is the
// single place that energizes the motor. A move runs continuously until the
// dead-reckoned position reaches the target, a soft end stop (0 %/100 %) or the
// free-run deadline. There are no pulses, durations or cooldowns.
void stop_mixer(SystemState& state) {
  if (!state.mix_motion) {
    return;
  }
  l0::set_mixer_motor(false, state.mix_dir > 0);
  state.mix_motion = false;
  state.mix_dir = 0;
}

// Energize the motor in `want_dir`. If the relays were last driven the other
// way, drop the motion, wait for the contacts to release, then switch direction
// and start again.
void start_mixer(SystemState& state, int8_t want_dir) {
  if (g_mix_last_dir != 0 && want_dir != g_mix_last_dir) {
    l0::set_mixer_motor(false, g_mix_last_dir > 0);
    delay(kMixerDirSwitchMs);
  }
  l0::set_mixer_motor(true, want_dir > 0);
  g_mix_last_dir = want_dir;
  state.mix_motion = true;
  state.mix_dir = want_dir;
  g_mixer_last_ms = millis();
}

void service_mixer(const Config& config, SystemState& state, bool force_close, bool allow_open) {
  const uint32_t now = millis();
  const float ceiling = mixer_ceiling(config);

  // The ceiling is absolute: even a stale target or a maintenance sweep cannot
  // command the valve past it. Force-close always wins.
  float target = force_close ? 0.0f : state.mix_target_pos;
  if (!isfinite(target) || target < 0.0f) {
    target = 0.0f;
  } else if (target > 1.0f) {
    target = 1.0f;
  }
  if (!force_close && target > ceiling) {
    target = ceiling;
  }
  const int8_t free_dir = force_close ? 0 : state.mix_free_run_dir;

  if (state.mix_motion) {
    const uint32_t dt = now - g_mixer_last_ms;
    g_mixer_last_ms = now;

    const uint32_t travel_ms = config.travel_time_ms > 0 ? config.travel_time_ms : 145000;
    const float step = static_cast<float>(dt) / static_cast<float>(travel_ms);
    float next = state.pos_est_dr + (state.mix_dir > 0 ? step : -step);

    bool stop = false;
    if (free_dir != 0) {
      // Free-run (homing/sweep): ignore the target and the soft limits, stop
      // when the request changes or the deadline passes.
      if (free_dir != state.mix_dir) {
        stop = true;
      } else if (state.mix_free_run_until_ms == 0 ||
                 static_cast<int32_t>(now - state.mix_free_run_until_ms) >= 0) {
        stop = true;
        state.mix_free_run_dir = 0;
        state.mix_free_run_until_ms = 0;
      } else if (state.mix_dir > 0 && next >= ceiling) {
        // A sweep may exercise the valve, but only up to the configured
        // opening limit. End the free-run so it does not restart on the next
        // tick and pulse against the cap.
        next = ceiling;
        stop = true;
        state.mix_free_run_dir = 0;
        state.mix_free_run_until_ms = 0;
      }
    } else {
      if (next <= 0.0f) {
        next = 0.0f;
        stop = true;
      } else if (next >= 1.0f) {
        next = 1.0f;
        stop = true;
      }
      if (!allow_open && state.mix_dir > 0) {
        stop = true;
      } else if (state.mix_dir > 0 ? next >= target : next <= target) {
        stop = true;
      }
    }
    // Backstop for any opening move, including a free-run sweep.
    if (state.mix_dir > 0 && next > ceiling) {
      next = ceiling;
      stop = true;
    }
    state.pos_est_dr = next;

    if (stop) {
      stop_mixer(state);
    }
    return;
  }

  g_mixer_last_ms = now;

  if (free_dir != 0) {
    if (state.mix_free_run_until_ms == 0 ||
        static_cast<int32_t>(now - state.mix_free_run_until_ms) >= 0) {
      // Expired or unbounded free-run request: self-heal instead of restarting.
      state.mix_free_run_dir = 0;
      state.mix_free_run_until_ms = 0;
      return;
    }
    start_mixer(state, free_dir);
    return;
  }

  const float error = target - state.pos_est_dr;
  if (fabsf(error) <= config.deadband_pos) {
    return;
  }
  const int8_t want_dir = error > 0.0f ? 1 : -1;
  if (want_dir > 0 && !allow_open) {
    return;
  }
  start_mixer(state, want_dir);
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)state;
  Serial.printf("[l1] mixer opening limit %u%%\n",
                static_cast<unsigned>(config.mix_max_open_pct));
  g_mix_last_dir = 0;
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
  if (heater_req && !any_pump) {
    state.active_alarms |= WARN_HEATER_NO_PUMP;
  } else {
    state.active_alarms &= ~WARN_HEATER_NO_PUMP;
  }

  apply_relay(Relay::PumpFloor, floor_req, state.pump_floor_phys, "pump_floor");
  apply_relay(Relay::PumpRad1, rad1_req, state.pump_rad1_phys, "pump_rad1");
  apply_relay(Relay::PumpRad2, rad2_req, state.pump_rad2_phys, "pump_rad2");
  // Plan 03 §2.7 permits heater-on with all pumps off; L2 long_pause_s is the
  // backstop that turns it off. L1 raises the diagnostic warning instead.
  apply_relay(Relay::Heater, heater_req, state.heater_phys, "heater");

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

  // --- Mixer servo ---------------------------------------------------------
  // Emergency (over-temperature, local sensor fault or welded relay) is not a
  // special movement: it just commands the safe position and locks every other
  // writer out while it is active.
  state.mix_emergency = force_mixer_close;
  if (force_mixer_close) {
    state.mix_target_pos = 0.0f;
    state.mix_free_run_dir = 0;
    state.mix_free_run_until_ms = 0;
  }
  service_mixer(config, state, force_mixer_close, allow_mixer_open);
}

}  // namespace l1
}  // namespace hpn
