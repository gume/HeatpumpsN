#include "hpn/tasks.h"

#include <Arduino.h>
#include <esp_task_wdt.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "hpn/build_target.h"
#include "hpn/l0_io.h"
#include "hpn/l1_safety.h"
#include "hpn/l2_control.h"
#include "hpn/l4_comms.h"
#include "hpn/logger.h"

namespace hpn {
namespace tasks {
namespace {

enum class TaskId : uint8_t {
  Safety = 0,
  Io,
  Control,
  Comms,
  Logger,
  Count,
};

constexpr size_t kTaskCount = static_cast<size_t>(TaskId::Count);

constexpr uint32_t kSafetyPeriodMs = 500;
constexpr uint32_t kIoPeriodMs = 20;
constexpr uint32_t kCommsPeriodMs = 50;
constexpr uint32_t kLoggerPeriodMs = 1000;

constexpr uint32_t kSafetyStack = 4096;
constexpr uint32_t kIoStack = 4096;
constexpr uint32_t kControlStack = 4096;
constexpr uint32_t kCommsStack = 6144;
constexpr uint32_t kLoggerStack = 3072;

constexpr UBaseType_t kPrioSafety = configMAX_PRIORITIES - 1;
constexpr UBaseType_t kPrioIo = configMAX_PRIORITIES - 2;
constexpr UBaseType_t kPrioControl = configMAX_PRIORITIES - 3;
constexpr UBaseType_t kPrioComms = tskIDLE_PRIORITY + 3;
constexpr UBaseType_t kPrioLogger = tskIDLE_PRIORITY + 1;

const Config* g_config = nullptr;
const BoardConfig* g_board = nullptr;
SystemState* g_state = nullptr;

TaskHandle_t g_handles[kTaskCount] = {};
volatile TickType_t g_last_seen[kTaskCount] = {};
volatile uint8_t g_seen_mask = 0;
portMUX_TYPE g_seen_mux = portMUX_INITIALIZER_UNLOCKED;

TickType_t g_start_tick = 0;
uint32_t g_control_period_ms = 2000;
bool g_started = false;

bool g_wdt_ready = false;
bool g_wdt_subscribed = false;

void mark(TaskId id) {
  const size_t index = static_cast<size_t>(id);
  const uint8_t bit = static_cast<uint8_t>(1u << index);

  taskENTER_CRITICAL(&g_seen_mux);
  g_last_seen[index] = xTaskGetTickCount();
  g_seen_mask |= bit;
  taskEXIT_CRITICAL(&g_seen_mux);
}

bool health_ok() {
  const TickType_t now = xTaskGetTickCount();
  const uint8_t required = static_cast<uint8_t>((1u << static_cast<size_t>(TaskId::Safety)) |
                                                (1u << static_cast<size_t>(TaskId::Io)) |
                                                (1u << static_cast<size_t>(TaskId::Control)));
  bool ok = false;

  taskENTER_CRITICAL(&g_seen_mux);
  if ((g_seen_mask & required) == required) {
    const TickType_t safety_age = now - g_last_seen[static_cast<size_t>(TaskId::Safety)];
    const TickType_t io_age = now - g_last_seen[static_cast<size_t>(TaskId::Io)];
    const TickType_t control_age = now - g_last_seen[static_cast<size_t>(TaskId::Control)];

    ok = (safety_age <= pdMS_TO_TICKS(kSafetyPeriodMs * 3)) &&
         (io_age <= pdMS_TO_TICKS(kIoPeriodMs * 10)) &&
         (control_age <= pdMS_TO_TICKS(g_control_period_ms * 3));
  }
  taskEXIT_CRITICAL(&g_seen_mux);

  return ok;
}

void watchdog_begin(uint32_t timeout_ms) {
  esp_task_wdt_config_t config = {timeout_ms, 0, true};
  esp_err_t err = esp_task_wdt_init(&config);
  if (err == ESP_ERR_INVALID_STATE) {
    err = esp_task_wdt_reconfigure(&config);
  }

  g_wdt_ready = (err == ESP_OK);
  if (g_wdt_ready) {
    Serial.printf("[rtos] task WDT armed at %ums\n", static_cast<unsigned>(timeout_ms));
  } else {
    Serial.printf("[rtos] task WDT unavailable: %s (software supervision only)\n",
                  esp_err_to_name(err));
  }
}

void watchdog_subscribe_current() {
  if (!g_wdt_ready) {
    return;
  }

  const esp_err_t err = esp_task_wdt_add(nullptr);
  g_wdt_subscribed = (err == ESP_OK);
  if (!g_wdt_subscribed) {
    Serial.printf("[rtos] task WDT subscribe failed: %s\n", esp_err_to_name(err));
  }
}

void task_safety(void* arg) {
  (void)arg;
  for (;;) {
    l1::tick(*g_config, *g_state);
    mark(TaskId::Safety);
    vTaskDelay(pdMS_TO_TICKS(kSafetyPeriodMs));
  }
}

void task_io(void* arg) {
  (void)arg;
  watchdog_subscribe_current();

  for (;;) {
    l0::tick(*g_config, *g_board, *g_state);
    g_state->uptime_ms = millis();
    mark(TaskId::Io);

    const TickType_t now = xTaskGetTickCount();
    const bool startup_grace = (now - g_start_tick) < pdMS_TO_TICKS(2000);
    if (g_wdt_subscribed && (startup_grace || health_ok())) {
      esp_task_wdt_reset();
    }

    vTaskDelay(pdMS_TO_TICKS(kIoPeriodMs));
  }
}

void task_control(void* arg) {
  (void)arg;
  for (;;) {
    l2::tick(*g_config, *g_state);
    mark(TaskId::Control);
    vTaskDelay(pdMS_TO_TICKS(g_control_period_ms));
  }
}

void task_comms(void* arg) {
  (void)arg;
  for (;;) {
    l4::tick(*g_config, *g_state);
    mark(TaskId::Comms);
    vTaskDelay(pdMS_TO_TICKS(kCommsPeriodMs));
  }
}

void task_logger(void* arg) {
  (void)arg;
  for (;;) {
    logger::tick(*g_config, *g_state);
    mark(TaskId::Logger);
    vTaskDelay(pdMS_TO_TICKS(kLoggerPeriodMs));
  }
}

BaseType_t create_task(TaskFunction_t function, const char* name, uint32_t stack_bytes,
                       UBaseType_t priority, BaseType_t core, TaskHandle_t* handle) {
#if HPN_HAS_DUAL_CORE
  return xTaskCreatePinnedToCore(function, name, stack_bytes, nullptr, priority, handle, core);
#else
  (void)core;
  return xTaskCreate(function, name, stack_bytes, nullptr, priority, handle);
#endif
}

void delete_created_tasks() {
  if (g_wdt_subscribed && g_handles[static_cast<size_t>(TaskId::Io)] != nullptr) {
    esp_task_wdt_delete(g_handles[static_cast<size_t>(TaskId::Io)]);
    g_wdt_subscribed = false;
  }

  for (size_t i = 0; i < kTaskCount; ++i) {
    if (g_handles[i] != nullptr) {
      vTaskDelete(g_handles[i]);
      g_handles[i] = nullptr;
    }
  }
}

}  // namespace

bool start(const Config& config, const BoardConfig& board, SystemState& state) {
  if (g_started) {
    return true;
  }

  g_config = &config;
  g_board = &board;
  g_state = &state;
  g_start_tick = xTaskGetTickCount();
  g_seen_mask = 0;
  for (size_t i = 0; i < kTaskCount; ++i) {
    g_last_seen[i] = 0;
    g_handles[i] = nullptr;
  }

  g_control_period_ms = config.tick_ms >= 100 ? config.tick_ms : 100;

  l1::begin(config, state);
  l2::begin(config, state);
  l4::begin(config, state);
  logger::begin(config, state);

  const uint32_t watchdog_timeout_ms =
      (g_control_period_ms * 4) + 1000 > 5000 ? (g_control_period_ms * 4) + 1000 : 5000;
  watchdog_begin(watchdog_timeout_ms);

#if HPN_HAS_DUAL_CORE
  const BaseType_t control_core = 1;
  const BaseType_t protocol_core = 0;
  Serial.println("[rtos] dual-core target: real-time on Core 1, comms/logging on Core 0");
#else
  const BaseType_t control_core = 0;
  const BaseType_t protocol_core = 0;
  Serial.println("[rtos] single-core target: all tasks on Core 0");
#endif

  bool ok = true;
  ok = ok &&
       create_task(task_safety, "t_safety", kSafetyStack, kPrioSafety, control_core,
                   &g_handles[static_cast<size_t>(TaskId::Safety)]) == pdPASS;
  ok = ok &&
       create_task(task_io, "t_io", kIoStack, kPrioIo, control_core,
                   &g_handles[static_cast<size_t>(TaskId::Io)]) == pdPASS;
  ok = ok &&
       create_task(task_control, "t_control", kControlStack, kPrioControl, control_core,
                   &g_handles[static_cast<size_t>(TaskId::Control)]) == pdPASS;
  ok = ok &&
       create_task(task_comms, "t_comms", kCommsStack, kPrioComms, protocol_core,
                   &g_handles[static_cast<size_t>(TaskId::Comms)]) == pdPASS;
  ok = ok &&
       create_task(task_logger, "t_logger", kLoggerStack, kPrioLogger, protocol_core,
                   &g_handles[static_cast<size_t>(TaskId::Logger)]) == pdPASS;

  if (!ok) {
    Serial.println("[rtos] task creation failed; holding safe state");
    delete_created_tasks();
    return false;
  }

  g_started = true;
  Serial.printf("[rtos] tasks started: safety=%ums io=%ums control=%ums comms=%ums logger=%ums\n",
                static_cast<unsigned>(kSafetyPeriodMs), static_cast<unsigned>(kIoPeriodMs),
                static_cast<unsigned>(g_control_period_ms),
                static_cast<unsigned>(kCommsPeriodMs), static_cast<unsigned>(kLoggerPeriodMs));
  return true;
}

bool healthy() { return g_started && health_ok(); }

}  // namespace tasks
}  // namespace hpn
