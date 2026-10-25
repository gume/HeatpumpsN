#include "hpn/sim.h"

#include <Arduino.h>
#include <string.h>

#if defined(HPN_IO_EMULATION)

namespace hpn {
namespace sim {
namespace {

struct Model {
  Config config;
  BoardConfig board;

  uint8_t pin_level[kMaxGpio];  // 0 = low, 1 = high
  uint8_t output_byte;          // 74HC595 latch value

  float temperature_c[kTempRoleCount];
  bool temperature_valid[kTempRoleCount];
  float power_w[kCurrentChannelCount];

  uint16_t led_count;
  uint8_t led_rgb[kMaxLeds * 3];

  uint32_t revision;
};

Model g_model{};
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;

bool is_assigned(uint8_t pin) {
  return pin != HPN_GPIO_UNASSIGNED;
}

uint8_t relay_bit(const BoardConfig& board, Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return board.sr_bit_heater;
    case Relay::PumpRad1:
      return board.sr_bit_pump_rad1;
    case Relay::PumpRad2:
      return board.sr_bit_pump_rad2;
    case Relay::PumpFloor:
      return board.sr_bit_pump_floor;
    default:
      return HPN_GPIO_UNASSIGNED;
  }
}

uint8_t relay_pin(const Config& config, Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return config.gpio_heater;
    case Relay::PumpRad1:
      return config.gpio_pump_rad1;
    case Relay::PumpRad2:
      return config.gpio_pump_rad2;
    case Relay::PumpFloor:
      return config.gpio_pump_floor;
    default:
      return HPN_GPIO_UNASSIGNED;
  }
}

bool bit_is_set(uint8_t value, uint8_t bit) {
  if (bit >= 8) {
    return false;
  }
  return (value & static_cast<uint8_t>(1u << bit)) != 0;
}

bool pin_is_high(const Model& model, uint8_t pin) {
  return is_assigned(pin) && pin < kMaxGpio && model.pin_level[pin] != 0;
}

bool relay_state(const Model& model, Relay relay) {
  if (model.board.output_backend == OutputBackend::ShiftRegister) {
    return bit_is_set(model.output_byte, relay_bit(model.board, relay));
  }
  return pin_is_high(model, relay_pin(model.config, relay));
}

bool mixer_motion(const Model& model) {
  if (model.board.output_backend == OutputBackend::ShiftRegister) {
    return bit_is_set(model.output_byte, model.board.sr_bit_mix_motion);
  }
  // Direct carriers use two separate, interlocked relays.
  return pin_is_high(model, model.config.gpio_mix_open) ||
         pin_is_high(model, model.config.gpio_mix_close);
}

bool mixer_open(const Model& model) {
  if (model.board.output_backend == OutputBackend::ShiftRegister) {
    return bit_is_set(model.output_byte, model.board.sr_bit_mix_direction);
  }
  return pin_is_high(model, model.config.gpio_mix_open);
}

uint8_t mux_channel(const Model& model) {
  if (!model.board.has_current_mux || model.board.output_backend != OutputBackend::ShiftRegister) {
    return 0xFF;
  }
  uint8_t channel = 0;
  if (bit_is_set(model.output_byte, model.board.sr_bit_mux_s0)) {
    channel |= 0x01;
  }
  if (bit_is_set(model.output_byte, model.board.sr_bit_mux_s1)) {
    channel |= 0x02;
  }
  return channel;
}

bool output_enabled(const Model& model) {
  if (model.board.output_backend != OutputBackend::ShiftRegister) {
    return true;
  }
  if (!is_assigned(model.board.sr_oe_pin) || model.board.sr_oe_pin >= kMaxGpio) {
    return true;  // OE hard-wired on the carrier
  }
  const bool oe_high = model.pin_level[model.board.sr_oe_pin] != 0;
  return model.board.sr_oe_active_low ? !oe_high : oe_high;
}

void reset_locked(Model& model) {
  model.output_byte = 0;
  memset(model.pin_level, 0, sizeof(model.pin_level));

  for (uint8_t i = 0; i < kTempRoleCount; ++i) {
    model.temperature_valid[i] = true;
  }
  model.temperature_c[static_cast<size_t>(TempRole::Boiler)] = 45.0f;
  model.temperature_c[static_cast<size_t>(TempRole::ReturnFloor)] = 30.0f;
  model.temperature_c[static_cast<size_t>(TempRole::MixOut)] = 35.0f;

  for (uint8_t i = 0; i < kCurrentChannelCount; ++i) {
    model.power_w[i] = 0.0f;
  }

  model.led_count = 0;
  memset(model.led_rgb, 0, sizeof(model.led_rgb));

  // Mirror the power-on state of an un-driven 74HC595 carrier: outputs are
  // disabled until L0 pulls OE to its enable level.
  if (model.board.output_backend == OutputBackend::ShiftRegister &&
      is_assigned(model.board.sr_oe_pin) && model.board.sr_oe_pin < kMaxGpio) {
    model.pin_level[model.board.sr_oe_pin] = model.board.sr_oe_active_low ? 1 : 0;
  }

  ++model.revision;
}

void touch(Model& model) {
  ++model.revision;
}

bool valid_temp_role(TempRole role) {
  return static_cast<uint8_t>(role) < kTempRoleCount;
}

bool valid_channel(CurrentChannel channel) {
  return static_cast<uint8_t>(channel) < kCurrentChannelCount;
}

}  // namespace

void begin(const Config& config, const BoardConfig& board) {
  taskENTER_CRITICAL(&g_mux);
  g_model.config = config;
  g_model.board = board;
  g_model.revision = 0;
  reset_locked(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

Snapshot snapshot() {
  Snapshot out{};

  taskENTER_CRITICAL(&g_mux);
  const Model& model = g_model;

  out.revision = model.revision;
  for (uint8_t i = 0; i < kTempRoleCount; ++i) {
    out.temperature_c[i] = model.temperature_c[i];
    out.temperature_valid[i] = model.temperature_valid[i];
  }
  for (uint8_t i = 0; i < kCurrentChannelCount; ++i) {
    out.power_w[i] = model.power_w[i];
  }
  for (uint8_t i = 0; i < kRelayCount; ++i) {
    out.relay_on[i] = relay_state(model, static_cast<Relay>(i));
  }
  out.mixer_motion = mixer_motion(model);
  out.mixer_open = mixer_open(model);
  out.mux_channel = mux_channel(model);
  out.output_byte = model.output_byte;
  out.output_enabled = output_enabled(model);
  out.led_count = model.led_count;
  memcpy(out.led_rgb, model.led_rgb, sizeof(out.led_rgb));

  taskEXIT_CRITICAL(&g_mux);
  return out;
}

void set_temperature(TempRole role, float celsius, bool valid) {
  if (!valid_temp_role(role)) {
    return;
  }
  const size_t index = static_cast<size_t>(role);

  taskENTER_CRITICAL(&g_mux);
  g_model.temperature_c[index] = celsius;
  g_model.temperature_valid[index] = valid;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_power_w(CurrentChannel channel, float watts) {
  if (!valid_channel(channel)) {
    return;
  }
  const size_t index = static_cast<size_t>(channel);

  taskENTER_CRITICAL(&g_mux);
  g_model.power_w[index] = watts > 0.0f ? watts : 0.0f;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void reset() {
  taskENTER_CRITICAL(&g_mux);
  reset_locked(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void record_pin(uint8_t pin, bool high) {
  if (pin >= kMaxGpio) {
    return;
  }
  taskENTER_CRITICAL(&g_mux);
  g_model.pin_level[pin] = high ? 1 : 0;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void record_shift_register(uint8_t value) {
  taskENTER_CRITICAL(&g_mux);
  g_model.output_byte = value;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void record_led_count(uint16_t count) {
  taskENTER_CRITICAL(&g_mux);
  g_model.led_count = count > kMaxLeds ? kMaxLeds : count;
  memset(g_model.led_rgb, 0, sizeof(g_model.led_rgb));
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void record_led_frame(const uint8_t* rgb, uint16_t count) {
  if (rgb == nullptr) {
    return;
  }
  if (count > kMaxLeds) {
    count = kMaxLeds;
  }

  taskENTER_CRITICAL(&g_mux);
  memset(g_model.led_rgb, 0, sizeof(g_model.led_rgb));
  memcpy(g_model.led_rgb, rgb, static_cast<size_t>(count) * 3);
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

bool read_temperature(TempRole role, float& celsius) {
  if (!valid_temp_role(role)) {
    return false;
  }
  const size_t index = static_cast<size_t>(role);

  bool valid = false;
  taskENTER_CRITICAL(&g_mux);
  valid = g_model.temperature_valid[index];
  celsius = g_model.temperature_c[index];
  taskEXIT_CRITICAL(&g_mux);
  return valid;
}

float read_power_w(CurrentChannel channel) {
  if (!valid_channel(channel)) {
    return 0.0f;
  }
  const size_t index = static_cast<size_t>(channel);

  float watts = 0.0f;
  taskENTER_CRITICAL(&g_mux);
  watts = g_model.power_w[index];
  taskEXIT_CRITICAL(&g_mux);
  return watts;
}

}  // namespace sim
}  // namespace hpn

#endif  // HPN_IO_EMULATION
