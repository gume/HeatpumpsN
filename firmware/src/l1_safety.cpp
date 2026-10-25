#include "hpn/l1_safety.h"

#if defined(HPN_BENCH_LOOPBACK)
#include <Arduino.h>

#include "hpn/l0_io.h"
#endif

namespace hpn {
namespace l1 {

#if defined(HPN_BENCH_LOOPBACK)
namespace {

uint32_t g_mixer_end_ms = 0;

void apply_relay(Relay relay, bool requested, bool& physical, const char* name) {
  if (requested == physical) {
    return;
  }
  l0::set_relay(relay, requested);
  physical = requested;
  Serial.printf("[l1-bench] %s -> %s\n", name, requested ? "ON" : "OFF");
}

}  // namespace
#endif

void begin(const Config& config, SystemState& state) {
#if defined(HPN_BENCH_LOOPBACK)
  g_mixer_end_ms = 0;
#endif
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
#if defined(HPN_BENCH_LOOPBACK)
  // Bench-only loopback. This is deliberately NOT the L1 safety arbitration
  // described in plan 03: it only proves that portal commands, L2, L1 and L0
  // are wired together on the emulated carrier. The heater interlock below is
  // a minimal demonstration; over-temperature, gradient, sensor-posture and
  // demand/after-heating rules are still TODO(L1).
  const bool any_pump = state.pump_floor_requested || state.pump_rad1_requested ||
                        state.pump_rad2_requested;

  apply_relay(Relay::PumpFloor, state.pump_floor_requested, state.pump_floor_phys, "pump_floor");
  apply_relay(Relay::PumpRad1, state.pump_rad1_requested, state.pump_rad1_phys, "pump_rad1");
  apply_relay(Relay::PumpRad2, state.pump_rad2_requested, state.pump_rad2_phys, "pump_rad2");
  apply_relay(Relay::Heater, state.heater_requested && any_pump, state.heater_phys, "heater");

  if (state.heater_requested && !any_pump) {
    state.active_alarms |= WARN_HEATER_NO_PUMP;
  } else {
    state.active_alarms &= ~WARN_HEATER_NO_PUMP;
  }

  const uint32_t now = millis();
  if (state.mix_pulse_ms_phys > 0) {
    if (static_cast<int32_t>(now - g_mixer_end_ms) >= 0) {
      l0::set_mixer_motor(false, false);
      state.mix_pulse_ms_phys = 0;
      state.mix_pulse_dir_phys = 0;
      Serial.println("[l1-bench] mixer stop");
    } else {
      const uint32_t travel_ms = config.travel_time_ms > 0 ? config.travel_time_ms : 120000;
      const float step = 500.0f / static_cast<float>(travel_ms);
      state.pos_est_dr += state.mix_pulse_dir_phys > 0 ? step : -step;
      if (state.pos_est_dr < 0.0f) state.pos_est_dr = 0.0f;
      if (state.pos_est_dr > 1.0f) state.pos_est_dr = 1.0f;
    }
  } else if (state.mix_pulse_ms_req > 0) {
    const uint32_t pulse_ms = state.mix_pulse_ms_req;
    state.mix_pulse_ms_req = 0;
    state.mix_pulse_dir_phys = state.mix_pulse_dir_req > 0 ? 1 : -1;
    state.mix_pulse_ms_phys = pulse_ms;
    g_mixer_end_ms = now + pulse_ms;
    l0::set_mixer_motor(true, state.mix_pulse_dir_phys > 0);
    Serial.printf("[l1-bench] mixer %s pulse %ums\n",
                  state.mix_pulse_dir_phys > 0 ? "open" : "close",
                  static_cast<unsigned>(pulse_ms));
  }
#else
  // TODO(L1): evaluate over-temperature, gradient, sensor postures,
  // demand/after-heating timeouts, and drive l0::set_relay/set_mixer_motor.
  (void)config;
  (void)state;
#endif
}

}  // namespace l1
}  // namespace hpn
