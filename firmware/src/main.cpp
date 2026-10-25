#include <Arduino.h>

#include "hpn/build_target.h"
#include "hpn/config.h"
#include "hpn/l0_io.h"
#include "hpn/state.h"

namespace {

Config g_config;
SystemState g_state;
uint32_t g_last_heartbeat_ms = 0;

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(100);

  Serial.printf("\n\nHeatpumpsN controller (scaffold)\n");
  Serial.printf("target : %s / %s\n", HPN_TARGET_NAME, HPN_TARGET_FAMILY);
  Serial.printf("cores  : %s\n", HPN_HAS_DUAL_CORE ? "dual" : "single");
  Serial.printf("plan   : L0 boot-safe initialization only; L1/L2/L4 pending\n");

  g_config = config_defaults();
  state_init(g_state);

  // L0 is the only layer permitted to touch pins (plan 02 §2.1).
  hpn::l0::begin(g_config);
  hpn::l0::apply_safe_state();

  Serial.println("[boot] safe state applied");
}

void loop() {
  const uint32_t now_ms = millis();
  hpn::l0::tick();

  if (now_ms - g_last_heartbeat_ms >= 1000) {
    g_last_heartbeat_ms = now_ms;
    g_state.uptime_ms = now_ms;

    Serial.printf("[heartbeat] uptime=%lus mode=%d alarms=0x%08lx err=%u\n",
                  static_cast<unsigned long>(now_ms / 1000),
                  static_cast<int>(g_state.mode),
                  static_cast<unsigned long>(g_state.active_alarms),
                  static_cast<unsigned>(g_state.system_error_code));
  }

  delay(1);
}
