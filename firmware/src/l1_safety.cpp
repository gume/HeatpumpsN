#include "hpn/l1_safety.h"

#include <Arduino.h>

#include "hpn/l0_io.h"

namespace hpn {
namespace l1 {
namespace {

uint32_t g_mixer_end_ms = 0;
uint32_t g_mixer_last_ms = 0;

#if defined(HPN_BENCH_LOOPBACK)
void apply_relay(Relay relay, bool requested, bool& physical, const char* name) {
  if (requested == physical) {
    return;
  }
  l0::set_relay(relay, requested);
  physical = requested;
  Serial.printf("[l1-bench] %s -> %s\n", name, requested ? "ON" : "OFF");
}
#endif

// Execute one mixer pulse at a time and dead-reckon the valve position from the
// commanded direction and elapsed time. Reaching 0 % or 100 % is treated as a
// soft end stop and re-zeroes the estimate, which is the only calibration
// reference available until a physical endstop or encoder is installed.
void service_mixer(const Config& config, SystemState& state) {
  const uint32_t now = millis();
  if (g_mixer_last_ms == 0) {
    g_mixer_last_ms = now;
  }

  if (state.mix_pulse_ms_phys > 0) {
    const uint32_t dt = now - g_mixer_last_ms;
    g_mixer_last_ms = now;

    const uint32_t travel_ms = config.travel_time_ms > 0 ? config.travel_time_ms : 120000;
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
  if (state.mix_pulse_ms_req == 0) {
    return;
  }

  const uint32_t pulse_ms = state.mix_pulse_ms_req;
  state.mix_pulse_ms_req = 0;
  state.mix_pulse_dir_phys = state.mix_pulse_dir_req > 0 ? 1 : -1;
  state.mix_pulse_ms_phys = pulse_ms;
  g_mixer_end_ms = now + pulse_ms;
  g_mixer_last_ms = now;
  l0::set_mixer_motor(true, state.mix_pulse_dir_phys > 0);
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
  g_mixer_end_ms = 0;
  g_mixer_last_ms = 0;
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
#else
  // TODO(L1): evaluate over-temperature, gradient, sensor postures,
  // demand/after-heating timeouts, and drive l0::set_relay. The mixer pulse
  // path below still runs so bench and hardware builds share one actuator path.
#endif

  service_mixer(config, state);
}

}  // namespace l1
}  // namespace hpn
