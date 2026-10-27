#include "hpn/sim.h"

#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#if defined(HPN_IO_EMULATION)

namespace hpn {
namespace sim {
namespace {

// Placeholder boiler-supply shape. The real logged form will replace this;
// for now it is a slow sine wave between 60 and 70 °C.
constexpr float kBoilerSineCenterC = 65.0f;
constexpr float kBoilerSineAmplitudeC = 5.0f;
constexpr uint32_t kBoilerSinePeriodMs = 120000u;
constexpr float kTwoPi = 6.28318530718f;
constexpr float kMaxEmulatedPowerW = 40.0f;

struct Model {
  Config config;
  BoardConfig board;

  uint8_t pin_level[kMaxGpio];  // 0 = low, 1 = high
  uint8_t output_byte;          // last expander output byte

  float temperature_set_c[kTempRoleCount];
  bool temperature_valid[kTempRoleCount];
  float power_set_w[kCurrentChannelCount];

  bool follow_controller;
  bool realistic_power;
  bool realistic_boiler;
  bool interlock_fault;
  bool relay_override[kRelayCount];
  bool relay_manual_on[kRelayCount];
  bool mixer_override;
  bool mixer_manual_motion;
  bool mixer_manual_open;
  float mixer_position;
  uint32_t mixer_last_ms;

  uint16_t led_count;
  uint8_t led_rgb[kMaxLeds * 3];

  uint32_t revision;
};

Model g_model{};
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;

bool is_assigned(uint8_t pin) {
  return pin != HPN_GPIO_UNASSIGNED;
}

uint8_t relay_bit(const Config& config, Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return config.expander_bit_heater;
    case Relay::PumpRad1:
      return config.expander_bit_pump_rad1;
    case Relay::PumpRad2:
      return config.expander_bit_pump_rad2;
    case Relay::PumpFloor:
      return config.expander_bit_pump_floor;
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

bool uses_expander(const Config& config) {
  return config.output_expander == OUTPUT_EXPANDER_74HC595 ||
         config.output_expander == OUTPUT_EXPANDER_PCF8574;
}

bool output_bit_on(uint8_t byte, uint8_t bit, bool active_low) {
  const bool high = bit_is_set(byte, bit);
  return active_low ? !high : high;
}

bool relay_active_low(const Config& config, Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return config.output_active_low_heater;
    case Relay::PumpRad1:
      return config.output_active_low_pump_rad1;
    case Relay::PumpRad2:
      return config.output_active_low_pump_rad2;
    case Relay::PumpFloor:
      return config.output_active_low_pump_floor;
    default:
      return false;
  }
}

bool pin_output_on(const Model& model, uint8_t pin, bool active_low) {
  if (!is_assigned(pin) || pin >= kMaxGpio) {
    return false;
  }
  const bool high = model.pin_level[pin] != 0;
  return active_low ? !high : high;
}

bool controller_relay_state(const Model& model, Relay relay) {
  if (uses_expander(model.config)) {
    return output_bit_on(model.output_byte, relay_bit(model.config, relay),
                         relay_active_low(model.config, relay));
  }
  return pin_output_on(model, relay_pin(model.config, relay),
                       relay_active_low(model.config, relay));
}

bool controller_mixer_motion(const Model& model) {
  const bool open_close = model.config.mixer_wiring == MIX_WIRING_OPEN_CLOSE;
  const bool direction_low = model.config.output_active_low_mix_direction;
  const bool motion_low = model.config.output_active_low_mix_motion;
  if (uses_expander(model.config)) {
    const bool first = output_bit_on(model.output_byte,
                                     model.config.expander_bit_mix_direction, direction_low);
    const bool second = output_bit_on(model.output_byte,
                                      model.config.expander_bit_mix_motion, motion_low);
    return open_close ? (first || second) : second;
  }
  const bool first = pin_output_on(model, model.config.gpio_mix_open, direction_low);
  const bool second = pin_output_on(model, model.config.gpio_mix_close, motion_low);
  // New board: two interlocked open/close relays. Legacy board: the first
  // pin is direction and the second is the motion enable.
  return open_close ? (first || second) : second;
}

bool controller_mixer_open(const Model& model) {
  const bool direction_low = model.config.output_active_low_mix_direction;
  if (uses_expander(model.config)) {
    return output_bit_on(model.output_byte,
                         model.config.expander_bit_mix_direction, direction_low);
  }
  return pin_output_on(model, model.config.gpio_mix_open, direction_low);
}

bool effective_relay_state(const Model& model, Relay relay) {
  const size_t index = static_cast<size_t>(relay);
  if (model.relay_override[index]) {
    return model.relay_manual_on[index];
  }
  if (!model.follow_controller) {
    return model.relay_manual_on[index];
  }
  return controller_relay_state(model, relay);
}

bool effective_mixer_motion(const Model& model) {
  if (model.mixer_override) {
    return model.mixer_manual_motion;
  }
  if (!model.follow_controller) {
    return model.mixer_manual_motion;
  }
  return controller_mixer_motion(model);
}

bool effective_mixer_open(const Model& model) {
  if (model.mixer_override) {
    return model.mixer_manual_open;
  }
  if (!model.follow_controller) {
    return model.mixer_manual_open;
  }
  return controller_mixer_open(model);
}

float effective_temperature(const Model& model, TempRole role, uint32_t now_ms) {
  const size_t index = static_cast<size_t>(role);
  if (role == TempRole::Boiler && model.realistic_boiler) {
    const float phase =
        kTwoPi * static_cast<float>(now_ms % kBoilerSinePeriodMs) /
        static_cast<float>(kBoilerSinePeriodMs);
    return kBoilerSineCenterC + kBoilerSineAmplitudeC * sinf(phase);
  }
  return model.temperature_set_c[index];
}

// Integrate the simulated valve position while the motor is energised. The
// controller's own dead-reckoning estimate lives in SystemState; this is the
// "physical" carrier position that the emulation page shows next to it.
void advance_mixer_locked(Model& model, uint32_t now_ms) {
  if (model.mixer_last_ms == 0) {
    model.mixer_last_ms = now_ms;
    return;
  }

  const uint32_t elapsed_ms = now_ms - model.mixer_last_ms;
  model.mixer_last_ms = now_ms;
  if (!effective_mixer_motion(model)) {
    return;
  }

  const uint32_t travel_ms =
      model.config.travel_time_ms > 0 ? model.config.travel_time_ms : 120000;
  const float delta = static_cast<float>(elapsed_ms) / static_cast<float>(travel_ms);
  model.mixer_position += effective_mixer_open(model) ? delta : -delta;
  if (model.mixer_position < 0.0f) {
    model.mixer_position = 0.0f;
  } else if (model.mixer_position > 1.0f) {
    model.mixer_position = 1.0f;
  }
}

Relay relay_for_channel(CurrentChannel channel) {
  switch (channel) {
    case CurrentChannel::PumpFloor:
      return Relay::PumpFloor;
    case CurrentChannel::PumpRad1:
      return Relay::PumpRad1;
    case CurrentChannel::PumpRad2:
      return Relay::PumpRad2;
    default:
      return Relay::Heater;
  }
}

float effective_power(const Model& model, CurrentChannel channel) {
  const size_t index = static_cast<size_t>(channel);
  if (!model.realistic_power) {
    return model.power_set_w[index];
  }
  if (channel == CurrentChannel::Mixer) {
    return effective_mixer_motion(model) ? model.power_set_w[index] : 0.0f;
  }
  return effective_relay_state(model, relay_for_channel(channel))
             ? model.power_set_w[index]
             : 0.0f;
}

uint8_t mux_channel(const Model& model) {
  if (!model.board.has_current_mux || !uses_expander(model.config)) {
    return 0xFF;
  }
  uint8_t channel = 0;
  if (bit_is_set(model.output_byte, model.config.expander_bit_mux_s0)) {
    channel |= 0x01;
  }
  if (bit_is_set(model.output_byte, model.config.expander_bit_mux_s1)) {
    channel |= 0x02;
  }
  return channel;
}

bool output_enabled(const Model& model) {
  if (model.config.output_expander != OUTPUT_EXPANDER_74HC595) {
    return true;  // direct GPIO, or a PCF8574 without an OE pin
  }
  if (!is_assigned(model.config.expander_oe_pin) || model.config.expander_oe_pin >= kMaxGpio) {
    return true;  // OE hard-wired on the carrier
  }
  const bool oe_high = model.pin_level[model.config.expander_oe_pin] != 0;
  return model.config.expander_oe_active_low ? !oe_high : oe_high;
}

void reset_locked(Model& model) {
  // Mirror the power-on state of the selected driver. PCF8574 pins idle high,
  // which is the safe OFF level for an active-low relay board and the required
  // released level for the fault input. The 74HC595 starts at 0.
  const Config& config = model.config;
  uint8_t initial_output = config.output_expander == OUTPUT_EXPANDER_PCF8574 ? 0xFF : 0x00;
  const bool levels[] = {
      config.output_active_low_heater,
      config.output_active_low_pump_rad1,
      config.output_active_low_pump_rad2,
      config.output_active_low_pump_floor,
      config.output_active_low_mix_direction,
      config.output_active_low_mix_motion,
  };
  const uint8_t bits[] = {
      config.expander_bit_heater,      config.expander_bit_pump_rad1,
      config.expander_bit_pump_rad2,   config.expander_bit_pump_floor,
      config.expander_bit_mix_direction, config.expander_bit_mix_motion,
  };
  for (size_t i = 0; i < sizeof(bits); ++i) {
    if (bits[i] < 8 && levels[i]) {
      initial_output |= static_cast<uint8_t>(1u << bits[i]);
    }
  }
  // The MUX select lines are active-high, independently of relay polarity.
  if (config.expander_bit_mux_s0 < 8) {
    initial_output &= static_cast<uint8_t>(~(1u << config.expander_bit_mux_s0));
  }
  if (config.expander_bit_mux_s1 < 8) {
    initial_output &= static_cast<uint8_t>(~(1u << config.expander_bit_mux_s1));
  }
  // The fault input (P7) is quasi-bidirectional: the port latch stays high.
  if (config.expander_bit_mix_fault < 8) {
    initial_output |= static_cast<uint8_t>(1u << config.expander_bit_mix_fault);
  }
  model.output_byte = initial_output;
  memset(model.pin_level, 0, sizeof(model.pin_level));

  for (uint8_t i = 0; i < kTempRoleCount; ++i) {
    model.temperature_valid[i] = true;
  }
  model.temperature_set_c[static_cast<size_t>(TempRole::Boiler)] = 45.0f;
  model.temperature_set_c[static_cast<size_t>(TempRole::ReturnFloor)] = 30.0f;
  model.temperature_set_c[static_cast<size_t>(TempRole::MixOut)] = 35.0f;

  for (uint8_t i = 0; i < kCurrentChannelCount; ++i) {
    model.power_set_w[i] = 10.0f;
  }
  model.power_set_w[static_cast<size_t>(CurrentChannel::Mixer)] = 2.0f;

  model.follow_controller = true;
  model.realistic_power = true;
  model.realistic_boiler = false;
  model.interlock_fault = false;
  for (uint8_t i = 0; i < kRelayCount; ++i) {
    model.relay_override[i] = false;
    model.relay_manual_on[i] = false;
  }
  model.mixer_override = false;
  model.mixer_manual_motion = false;
  model.mixer_manual_open = false;
  model.mixer_position = 0.0f;
  model.mixer_last_ms = 0;

  model.led_count = 0;
  memset(model.led_rgb, 0, sizeof(model.led_rgb));

  // Mirror the power-on state of an un-driven 74HC595 carrier: outputs are
  // disabled until L0 pulls OE to its enable level.
  if (model.config.output_expander == OUTPUT_EXPANDER_74HC595 &&
      is_assigned(model.config.expander_oe_pin) && model.config.expander_oe_pin < kMaxGpio) {
    model.pin_level[model.config.expander_oe_pin] =
        model.config.expander_oe_active_low ? 1 : 0;
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
  const uint32_t now_ms = millis();

  taskENTER_CRITICAL(&g_mux);
  advance_mixer_locked(g_model, now_ms);
  const Model& model = g_model;

  out.revision = model.revision;
  for (uint8_t i = 0; i < kTempRoleCount; ++i) {
    const TempRole role = static_cast<TempRole>(i);
    out.temperature_c[i] = effective_temperature(model, role, now_ms);
    out.temperature_valid[i] =
        (role == TempRole::Boiler && model.realistic_boiler) ? true
                                                             : model.temperature_valid[i];
  }
  for (uint8_t i = 0; i < kCurrentChannelCount; ++i) {
    out.power_w[i] = effective_power(model, static_cast<CurrentChannel>(i));
    out.power_set_w[i] = model.power_set_w[i];
  }
  for (uint8_t i = 0; i < kRelayCount; ++i) {
    out.relay_on[i] = effective_relay_state(model, static_cast<Relay>(i));
    out.relay_override[i] = model.relay_override[i];
    out.relay_manual_on[i] = model.relay_manual_on[i];
  }
  out.mixer_motion = effective_mixer_motion(model);
  out.mixer_open = effective_mixer_open(model);
  out.mixer_override = model.mixer_override;
  out.mixer_manual_motion = model.mixer_manual_motion;
  out.mixer_manual_open = model.mixer_manual_open;
  out.mixer_position = model.mixer_position;
  out.follow_controller = model.follow_controller;
  out.realistic_power = model.realistic_power;
  out.realistic_boiler = model.realistic_boiler;
  out.interlock_fault = model.interlock_fault;
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
  g_model.temperature_set_c[index] = celsius;
  g_model.temperature_valid[index] = valid;
  if (role == TempRole::Boiler) {
    // A manual boiler value is an explicit override of the sine-wave shape.
    g_model.realistic_boiler = false;
  }
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_power_w(CurrentChannel channel, float watts) {
  if (!valid_channel(channel)) {
    return;
  }
  const size_t index = static_cast<size_t>(channel);
  if (!isfinite(watts)) {
    return;
  }
  if (watts < 0.0f) {
    watts = 0.0f;
  } else if (watts > kMaxEmulatedPowerW) {
    watts = kMaxEmulatedPowerW;
  }

  taskENTER_CRITICAL(&g_mux);
  g_model.power_set_w[index] = watts;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_relay_manual(Relay relay, bool on) {
  if (static_cast<uint8_t>(relay) >= kRelayCount) {
    return;
  }
  const size_t index = static_cast<size_t>(relay);

  taskENTER_CRITICAL(&g_mux);
  g_model.relay_manual_on[index] = on;
  g_model.relay_override[index] = g_model.follow_controller;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_mixer_manual(bool motion, bool open) {
  taskENTER_CRITICAL(&g_mux);
  g_model.mixer_manual_motion = motion;
  g_model.mixer_manual_open = motion && open;
  g_model.mixer_override = g_model.follow_controller;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_follow_controller(bool on) {
  taskENTER_CRITICAL(&g_mux);
  if (on != g_model.follow_controller) {
    if (on) {
      for (uint8_t i = 0; i < kRelayCount; ++i) {
        g_model.relay_override[i] = false;
      }
      g_model.mixer_override = false;
    } else {
      for (uint8_t i = 0; i < kRelayCount; ++i) {
        g_model.relay_manual_on[i] =
            effective_relay_state(g_model, static_cast<Relay>(i));
        g_model.relay_override[i] = false;
      }
      g_model.mixer_manual_motion = effective_mixer_motion(g_model);
      g_model.mixer_manual_open =
          g_model.mixer_manual_motion ? effective_mixer_open(g_model) : false;
      g_model.mixer_override = false;
    }
    g_model.follow_controller = on;
    touch(g_model);
  }
  taskEXIT_CRITICAL(&g_mux);
}

void set_realistic_power(bool on) {
  taskENTER_CRITICAL(&g_mux);
  g_model.realistic_power = on;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_realistic_boiler(bool on) {
  taskENTER_CRITICAL(&g_mux);
  g_model.realistic_boiler = on;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void set_interlock_fault(bool asserted) {
  taskENTER_CRITICAL(&g_mux);
  g_model.interlock_fault = asserted;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void clear_manual_overrides() {
  taskENTER_CRITICAL(&g_mux);
  for (uint8_t i = 0; i < kRelayCount; ++i) {
    g_model.relay_override[i] = false;
  }
  g_model.mixer_override = false;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void clear_relay_override(Relay relay) {
  if (static_cast<uint8_t>(relay) >= kRelayCount) {
    return;
  }
  const size_t index = static_cast<size_t>(relay);
  taskENTER_CRITICAL(&g_mux);
  g_model.relay_override[index] = false;
  touch(g_model);
  taskEXIT_CRITICAL(&g_mux);
}

void clear_mixer_override() {
  taskENTER_CRITICAL(&g_mux);
  g_model.mixer_override = false;
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
  const uint32_t now_ms = millis();

  bool valid = false;
  taskENTER_CRITICAL(&g_mux);
  celsius = effective_temperature(g_model, role, now_ms);
  valid = (role == TempRole::Boiler && g_model.realistic_boiler)
              ? true
              : g_model.temperature_valid[static_cast<size_t>(role)];
  taskEXIT_CRITICAL(&g_mux);
  return valid;
}

float read_power_w(CurrentChannel channel) {
  if (!valid_channel(channel)) {
    return 0.0f;
  }

  float watts = 0.0f;
  taskENTER_CRITICAL(&g_mux);
  watts = effective_power(g_model, channel);
  taskEXIT_CRITICAL(&g_mux);
  return watts;
}

bool read_expander(uint8_t& value) {
  taskENTER_CRITICAL(&g_mux);
  uint8_t port = g_model.output_byte;
  const uint8_t bit = g_model.config.expander_bit_mix_fault;
  if (bit < 8) {
    // PCF8574 P7 is an input: the board pulls it to the asserted level when
    // both mixer channels are requested, otherwise it stays released. The
    // v1.x board asserts high, so a healthy line reads low.
    const bool asserted_high = !g_model.config.mix_fault_active_low;
    const bool input_high = g_model.interlock_fault ? asserted_high : !asserted_high;
    port = input_high ? static_cast<uint8_t>(port | (1u << bit))
                      : static_cast<uint8_t>(port & ~(1u << bit));
  }
  value = port;
  taskEXIT_CRITICAL(&g_mux);
  return true;
}

uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count) {
  if (out == nullptr || max_count == 0) {
    return 0;
  }

  const TempRole roles[] = {TempRole::Boiler, TempRole::ReturnFloor, TempRole::MixOut};
  const char* fallback_ids[] = {"sim-boiler", "sim-return", "sim-mix"};
  const uint32_t now_ms = millis();
  uint8_t count = 0;

  taskENTER_CRITICAL(&g_mux);
  for (uint8_t i = 0; i < kTempRoleCount && count < max_count; ++i) {
    const char* configured = nullptr;
    switch (roles[i]) {
      case TempRole::Boiler:
        configured = g_model.config.sensor_id_boiler;
        break;
      case TempRole::ReturnFloor:
        configured = g_model.config.sensor_id_ret;
        break;
      case TempRole::MixOut:
        configured = g_model.config.sensor_id_mix;
        break;
      default:
        break;
    }
    const char* id = (configured != nullptr && configured[0] != '\0') ? configured : fallback_ids[i];
    snprintf(out[count].id, sizeof(out[count].id), "%s", id);
    out[count].celsius = effective_temperature(g_model, roles[i], now_ms);
    out[count].valid = (roles[i] == TempRole::Boiler && g_model.realistic_boiler)
                           ? true
                           : g_model.temperature_valid[static_cast<size_t>(roles[i])];
    ++count;
  }
  taskEXIT_CRITICAL(&g_mux);

  return count;
}

}  // namespace sim
}  // namespace hpn

#endif  // HPN_IO_EMULATION
