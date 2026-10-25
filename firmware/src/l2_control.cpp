#include "hpn/l2_control.h"

#include <Arduino.h>
#include <math.h>

namespace hpn {
namespace l2 {
namespace {

uint32_t g_mix_next_req_ms = 0;

// Simple proportional position controller. The full PI loop and the
// coordination matrix are still TODO(L2); this closes the loop around the
// dead-reckoned mixer estimate so the target command is actually useful.
void service_mixer_target(const Config& config, SystemState& state) {
  if (state.mix_free_run || state.mix_pulse_ms_phys > 0 || state.mix_pulse_ms_req > 0) {
    return;
  }

  const uint32_t now = millis();
  if (g_mix_next_req_ms != 0 && static_cast<int32_t>(now - g_mix_next_req_ms) < 0) {
    return;
  }

  float target = state.mix_target_pos;
  if (target < 0.0f) target = 0.0f;
  if (target > 1.0f) target = 1.0f;
  const float error = target - state.pos_est_dr;
  if (fabsf(error) <= config.deadband_pos) {
    return;
  }

  const float travel_ms =
      config.travel_time_ms > 0 ? static_cast<float>(config.travel_time_ms) : 120000.0f;
  float pulse_ms = fabsf(error) * travel_ms * config.kp;
  if (pulse_ms < static_cast<float>(config.min_pulse_ms)) {
    pulse_ms = static_cast<float>(config.min_pulse_ms);
  }
  if (pulse_ms > static_cast<float>(config.max_pulse_ms)) {
    pulse_ms = static_cast<float>(config.max_pulse_ms);
  }
  if (pulse_ms < 1.0f) {
    pulse_ms = 1.0f;
  }

  state.mix_pulse_dir_req = error > 0.0f ? 1 : -1;
  state.mix_pulse_ms_req = static_cast<uint32_t>(pulse_ms + 0.5f);
  g_mix_next_req_ms = now + state.mix_pulse_ms_req + config.pulse_cooldown_ms + 50u;
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
  g_mix_next_req_ms = 0;
}

void tick(const Config& config, SystemState& state) {
#if defined(HPN_BENCH_LOOPBACK)
  // Bench-only pass-through. The real coordination matrix, flush state machine
  // and DHW handling are still TODO(L2); this keeps the portal outputs wired
  // through L2 to L1 on the emulated carrier.
  state.heater_requested = state.heater_intended;
  state.pump_floor_requested = state.pump_floor_intended;
  state.pump_rad1_requested = state.pump_rad1_intended;
  state.pump_rad2_requested = state.pump_rad2_intended;
#else
  // TODO(L2): coordination matrix, flush state machine and DHW handling.
#endif

  service_mixer_target(config, state);
}

}  // namespace l2
}  // namespace hpn
