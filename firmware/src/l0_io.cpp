#include "hpn/l0_io.h"

#include <Arduino.h>

#include "hpn/build_target.h"

namespace hpn {
namespace l0 {
namespace {

// TODO(hardware): confirm relay polarity once the output board is fixed.
// All outputs are treated as active-high here; the safe level is therefore LOW.
constexpr uint8_t RELAY_OFF_LEVEL = LOW;

struct OutputPins {
  uint8_t pump_floor;
  uint8_t pump_rad1;
  uint8_t pump_rad2;
  uint8_t heater;
  uint8_t mix_open;
  uint8_t mix_close;
};

OutputPins g_outputs{};

bool is_assigned(uint8_t pin) { return pin != HPN_GPIO_UNASSIGNED; }

void init_output(uint8_t pin, const char* name) {
  if (!is_assigned(pin)) {
    Serial.printf("[l0] %-11s unassigned\n", name);
    return;
  }

  // Drive the safe level before enabling the output to avoid a boot glitch.
  digitalWrite(pin, RELAY_OFF_LEVEL);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, RELAY_OFF_LEVEL);

  Serial.printf("[l0] %-11s GPIO%u (safe=OFF)\n", name, static_cast<unsigned>(pin));
}

void write_off(uint8_t pin) {
  if (is_assigned(pin)) {
    digitalWrite(pin, RELAY_OFF_LEVEL);
  }
}

}  // namespace

void begin(const Config& config) {
  g_outputs.pump_floor = config.gpio_pump_floor;
  g_outputs.pump_rad1 = config.gpio_pump_rad1;
  g_outputs.pump_rad2 = config.gpio_pump_rad2;
  g_outputs.heater = config.gpio_heater;
  g_outputs.mix_open = config.gpio_mix_open;
  g_outputs.mix_close = config.gpio_mix_close;

  init_output(g_outputs.pump_floor, "pump_floor");
  init_output(g_outputs.pump_rad1, "pump_rad1");
  init_output(g_outputs.pump_rad2, "pump_rad2");
  init_output(g_outputs.heater, "heater");
  init_output(g_outputs.mix_open, "mix_open");
  init_output(g_outputs.mix_close, "mix_close");
}

void apply_safe_state() {
  write_off(g_outputs.pump_floor);
  write_off(g_outputs.pump_rad1);
  write_off(g_outputs.pump_rad2);
  write_off(g_outputs.heater);
  write_off(g_outputs.mix_open);
  write_off(g_outputs.mix_close);
}

void tick() {
  // Placeholder for the L0 pulse state machine, 1-Wire conversion scheduling,
  // encoder sampling, and pump-power plausibility checks.
}

}  // namespace l0
}  // namespace hpn
