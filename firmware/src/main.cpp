#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "hpn/board.h"
#include "hpn/build_target.h"
#include "hpn/config.h"
#include "hpn/l0_io.h"
#include "hpn/state.h"
#include "hpn/tasks.h"

namespace {

Config g_config;
SystemState g_state;
bool g_tasks_started = false;

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(100);

  const hpn::BoardConfig& board = hpn::board_config();
  g_config = config_defaults();
  state_init(g_state);

  Serial.printf("\n\nHeatpumpsN controller\n");
  Serial.printf("target : %s / %s\n", HPN_TARGET_NAME, HPN_TARGET_FAMILY);
  Serial.printf("cores  : %s\n", HPN_HAS_DUAL_CORE ? "dual" : "single");
  Serial.printf("board  : %s\n", board.name);

  // L0 is the only layer permitted to touch pins (plan 02 §2.1).
  hpn::l0::begin(g_config, board);
  hpn::l0::apply_safe_state();

  if (!hpn::tasks::start(g_config, board, g_state)) {
    Serial.println("[boot] RTOS startup failed; safe state held");
    return;
  }

  g_tasks_started = true;
  Serial.println("[boot] RTOS running; removing Arduino loop task");
  Serial.flush();

  // setup()/loop() run in the Arduino loopTask. Once the controller task set
  // exists, remove that extra task so only the planned tasks remain.
  vTaskDelete(nullptr);
}

void loop() {
  if (!g_tasks_started) {
    // Startup failure path: keep all outputs safe and report once per interval.
    hpn::l0::apply_safe_state();
    Serial.println("[fault] RTOS tasks not running; outputs held safe");
    delay(5000);
    return;
  }

  // Not reached after a successful boot (loopTask is deleted in setup()).
  vTaskDelay(pdMS_TO_TICKS(1000));
}
