// Host test for L2 floor coordination (warm-up flush, delta-T pause).
// Built and run by floor_simulator/tests/test_fw_l2.py:
//   g++ -std=c++17 -I../../include -Istubs -I<ArduinoJson>/src l2_harness.cpp ../../src/l2_control.cpp
#include <cstdio>
#include <cstring>

#include "hpn/config.h"
#include "hpn/l2_control.h"
#include "hpn/mpc.h"
#include "hpn/state.h"

static uint32_t g_ms = 1000;
uint32_t millis() { return g_ms; }
uint32_t micros() { return g_ms * 1000u; }

namespace hpn {
namespace web {
void log_event(uint8_t, uint16_t code, const char* detail) { std::printf("event %u %s\n", code, detail); }
}  // namespace web
// The predictive controller is not under test here: legacy mode.
namespace mpc {
void begin() {}
bool service_homing(const Config&, SystemState&) { return false; }
bool control(const Config&, SystemState&, bool) { return false; }
void floor_idle() {}
}  // namespace mpc
}  // namespace hpn

static void reading(SensorReading& r, float v) {
  r.value_c = v;
  r.is_valid = true;
  r.gradient_k_per_s = 0.0f;
}

int main() {
  Config c{};
  c.demand_timeout_s = 600;
  c.pump_stop_after_heating_s = 300;
  c.long_pause_s = 600;
  c.start_flush_ms = 30000;
  c.flush_max_pos = 0.2f;
  c.min_useful_delta_t = 2.0f;
  c.floor_pause_duration_s = 900;
  c.floor_setpoint_shadow = 35.0f;
  c.deadband_temp = 2.0f;
  c.kp = 0.5f;
  c.ki = 0.02f;
  c.ff_boiler = 0.47f;
  c.ff_return = 0.87f;
  c.mix_ff_offset = 6.75f;
  c.mix_max_open_pct = 85;
  c.mix_min_open_pct = 20;
  c.mix_overshoot_delta_c = 3.0f;
  SystemState s{};
  s.mode = MODE_HEAT;
  s.mix_ceiling = 0.85f;
  reading(s.T_boiler, 60.0f);
  reading(s.T_return_floor, 30.0f);
  reading(s.T_mix_out, 35.0f);
  hpn::l2::begin(c, s);
  s.pump_floor_intended = true;

  // L1 stand-in: the physical pump follows the request one tick later.
  auto tick = [&](int seconds) {
    for (int i = 0; i < seconds; i += 2) {
      g_ms += 2000;
      s.demand_age_s[CIRCUIT_FLOOR] = 0;   // keep-alive
      hpn::l2::tick(c, s);
      s.pump_floor_phys = s.pump_floor_requested;
    }
  };
  tick(2);
  std::printf("start flush=%d req=%d\n", s.flush_state == FLUSH_ACTIVE, s.pump_floor_requested);
  tick(40);
  std::printf("after40 flush=%d\n", s.flush_state == FLUSH_ACTIVE);
  reading(s.T_mix_out, 31.0f);                       // delta-T 1 K < 2 K
  tick(70);
  std::printf("collapse paused=%d req=%d\n", s.flush_state == FLUSH_PAUSED_DELTA_T, s.pump_floor_requested);
  tick(600);
  std::printf("mid paused=%d req=%d phys=%d\n", s.flush_state == FLUSH_PAUSED_DELTA_T,
              s.pump_floor_requested, s.pump_floor_phys);
  reading(s.T_mix_out, 35.0f);
  tick(320);                                         // pause (900 s) ends
  std::printf("restart flush=%d req=%d\n", s.flush_state == FLUSH_ACTIVE, s.pump_floor_requested);
  tick(40);
  std::printf("end flush=%d req=%d\n", s.flush_state == FLUSH_ACTIVE, s.pump_floor_requested);
  return 0;
}
