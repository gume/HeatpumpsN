#include "hpn/l0_io.h"

#include <Arduino.h>
#include <math.h>

#include "hpn/io_backend.h"

namespace hpn {
namespace l0 {
namespace {

// TODO(hardware): confirm the relay-driver polarity on HomeNet Heatpumps 1.0.
// The 74HC595 drives the relay board; active-high is assumed.
constexpr uint8_t kActuatorOnLevel = io::kHigh;
constexpr uint8_t kActuatorOffLevel = io::kLow;

Config g_config{};
BoardConfig g_board{};
uint8_t g_sr_cache = 0;
uint16_t g_led_count = 0;
bool g_leds_ready = false;
uint32_t g_last_temp_poll_ms = 0;
uint32_t g_last_power_poll_ms = 0;

bool is_assigned(uint8_t pin) {
  return pin != HPN_GPIO_UNASSIGNED;
}

uint8_t relay_bit(Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return g_board.sr_bit_heater;
    case Relay::PumpRad1:
      return g_board.sr_bit_pump_rad1;
    case Relay::PumpRad2:
      return g_board.sr_bit_pump_rad2;
    case Relay::PumpFloor:
      return g_board.sr_bit_pump_floor;
    default:
      return HPN_GPIO_UNASSIGNED;
  }
}

uint8_t relay_pin(Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return g_config.gpio_heater;
    case Relay::PumpRad1:
      return g_config.gpio_pump_rad1;
    case Relay::PumpRad2:
      return g_config.gpio_pump_rad2;
    case Relay::PumpFloor:
      return g_config.gpio_pump_floor;
    default:
      return HPN_GPIO_UNASSIGNED;
  }
}

void init_output_pin(uint8_t pin, const char* name) {
  if (!is_assigned(pin)) {
    Serial.printf("[l0] %-11s unassigned\n", name);
    return;
  }

  io::write_pin(pin, kActuatorOffLevel);
  io::pin_mode(pin, io::kModeOutput);
  io::write_pin(pin, kActuatorOffLevel);
  Serial.printf("[l0] %-11s GPIO%u (safe=OFF)\n", name, static_cast<unsigned>(pin));
}

// A 74HC595 with MSBFIRST puts bit 0 on Q0 (QA), matching the legacy ESPHome
// configuration.
void write_shift_register() {
  if (g_board.output_backend != OutputBackend::ShiftRegister) {
    return;
  }
  if (!is_assigned(g_board.sr_data_pin) || !is_assigned(g_board.sr_clock_pin) ||
      !is_assigned(g_board.sr_latch_pin)) {
    return;
  }

  io::shift_out_msb(g_board.sr_data_pin, g_board.sr_clock_pin, g_board.sr_latch_pin, g_sr_cache);
}

void init_shift_register() {
  if (!is_assigned(g_board.sr_data_pin) || !is_assigned(g_board.sr_clock_pin) ||
      !is_assigned(g_board.sr_latch_pin)) {
    Serial.println("[l0] shift-register pins missing; outputs stay disabled");
    return;
  }

  io::write_pin(g_board.sr_data_pin, io::kLow);
  io::pin_mode(g_board.sr_data_pin, io::kModeOutput);
  io::write_pin(g_board.sr_clock_pin, io::kLow);
  io::pin_mode(g_board.sr_clock_pin, io::kModeOutput);
  io::write_pin(g_board.sr_latch_pin, io::kLow);
  io::pin_mode(g_board.sr_latch_pin, io::kModeOutput);

  if (is_assigned(g_board.sr_oe_pin)) {
    const uint8_t disable_level = g_board.sr_oe_active_low ? io::kHigh : io::kLow;
    io::write_pin(g_board.sr_oe_pin, disable_level);
    io::pin_mode(g_board.sr_oe_pin, io::kModeOutput);
    io::write_pin(g_board.sr_oe_pin, disable_level);
  }

  // Zero every output before enabling the relay drivers.
  g_sr_cache = 0;
  write_shift_register();

  if (is_assigned(g_board.sr_oe_pin)) {
    const uint8_t enable_level = g_board.sr_oe_active_low ? io::kLow : io::kHigh;
    io::write_pin(g_board.sr_oe_pin, enable_level);
  }

  Serial.printf(
      "[l0] 74HC595 data=%u clock=%u latch=%u oe=%u\n", static_cast<unsigned>(g_board.sr_data_pin),
      static_cast<unsigned>(g_board.sr_clock_pin), static_cast<unsigned>(g_board.sr_latch_pin),
      static_cast<unsigned>(g_board.sr_oe_pin));
}

void init_direct_outputs() {
  init_output_pin(g_config.gpio_pump_floor, "pump_floor");
  init_output_pin(g_config.gpio_pump_rad1, "pump_rad1");
  init_output_pin(g_config.gpio_pump_rad2, "pump_rad2");
  init_output_pin(g_config.gpio_heater, "heater");
  init_output_pin(g_config.gpio_mix_open, "mix_open");
  init_output_pin(g_config.gpio_mix_close, "mix_close");
}

void init_current_mux() {
  if (!g_board.has_current_mux) {
    return;
  }

  if (is_assigned(g_board.adc_pin)) {
    io::adc_begin(g_board.adc_pin);
  }

  // 74HC4051 S2/S3 are held low on the HomeNet Heatpumps 1.0 carrier; only
  // channels 0-3 are used.
  if (is_assigned(g_board.mux_s2_pin)) {
    io::pin_mode(g_board.mux_s2_pin, io::kModeOutput);
    io::write_pin(g_board.mux_s2_pin, io::kLow);
  }
  if (g_board.mux_s3_pin != g_board.mux_s2_pin && is_assigned(g_board.mux_s3_pin)) {
    io::pin_mode(g_board.mux_s3_pin, io::kModeOutput);
    io::write_pin(g_board.mux_s3_pin, io::kLow);
  }

  io::adc_set_resolution(12);
  Serial.printf("[l0] current MUX adc=GPIO%u\n", static_cast<unsigned>(g_board.adc_pin));
}

void leds_off() {
  if (!g_leds_ready) {
    return;
  }
  uint8_t zeros[kMaxLeds * 3] = {};
  io::leds_show(zeros, g_led_count);
}

}  // namespace

void begin(const Config& config, const BoardConfig& board) {
  g_config = config;
  g_board = board;
  g_sr_cache = 0;
  g_leds_ready = false;
  g_led_count = 0;
  g_last_temp_poll_ms = 0;
  g_last_power_poll_ms = 0;

  Serial.printf("[l0] carrier   %s (%s)\n", g_board.name,
                g_board.output_backend == OutputBackend::ShiftRegister ? "74HC595" : "direct GPIO");
  Serial.printf("[l0] backend   %s\n", io::kEmulated ? "emulation" : "hardware");

  // Seed the emulation model (or the hardware backend) with this carrier.
  io::begin(g_config, g_board);

  if (g_board.output_backend == OutputBackend::ShiftRegister) {
    init_shift_register();
  } else {
    init_direct_outputs();
  }

  init_current_mux();

  if (is_assigned(g_config.gpio_led_data) && g_config.num_leds > 0) {
    g_led_count = g_config.num_leds > kMaxLeds ? kMaxLeds : g_config.num_leds;
    io::leds_begin(g_config.gpio_led_data, g_led_count);
    g_leds_ready = true;
    Serial.printf("[l0] LEDs      GPIO%u x%u\n", static_cast<unsigned>(g_config.gpio_led_data),
                  static_cast<unsigned>(g_led_count));
  }

  apply_safe_state();
}

void apply_safe_state() {
  if (g_board.output_backend == OutputBackend::ShiftRegister) {
    // All relays off, mixer motion off, direction low, MUX channel 0.
    g_sr_cache = 0;
    write_shift_register();
  } else {
    const uint8_t pins[] = {
        g_config.gpio_pump_floor, g_config.gpio_pump_rad1, g_config.gpio_pump_rad2,
        g_config.gpio_heater,     g_config.gpio_mix_open,  g_config.gpio_mix_close,
    };
    for (uint8_t pin : pins) {
      if (is_assigned(pin)) {
        io::write_pin(pin, kActuatorOffLevel);
      }
    }
  }

  leds_off();
}

void tick(const Config& config, const BoardConfig& board, SystemState& state) {
  const uint32_t now = millis();

  // Poll the temperature seam. On hardware the 1-Wire backend still reports
  // "no data"; under emulation these are the mocked values from hpn::sim.
  if (now - g_last_temp_poll_ms >= config.temp_poll_interval_ms) {
    g_last_temp_poll_ms = now;

    SensorReading* readings[] = {&state.T_boiler, &state.T_return_floor, &state.T_mix_out};
    const TempRole roles[] = {TempRole::Boiler, TempRole::ReturnFloor, TempRole::MixOut};

    for (uint8_t i = 0; i < 3; ++i) {
      float celsius = 0.0f;
      SensorReading& reading = *readings[i];
      if (read_temperature(roles[i], celsius) && isfinite(celsius)) {
        reading.value_c = celsius;
        reading.is_valid = true;
        reading.last_valid_ms = now;
        reading.consecutive_errors = 0;
      } else {
        reading.is_valid = false;
        if (reading.consecutive_errors < 255) {
          reading.consecutive_errors++;
        }
      }
    }
  }

  // Optional pump-power feedback, at the same cadence as the temperatures.
  if (now - g_last_power_poll_ms >= config.temp_poll_interval_ms) {
    g_last_power_poll_ms = now;
    state.pump_power_w[0] = read_power_w(CurrentChannel::PumpFloor);
    state.pump_power_w[1] = read_power_w(CurrentChannel::PumpRad1);
    state.pump_power_w[2] = read_power_w(CurrentChannel::PumpRad2);
    state.mixer_power_w = read_power_w(CurrentChannel::Mixer);
  }

  // The mixer pulse state machine and encoder sampling are still TODO(L0).
  (void)board;
}

void set_relay(Relay relay, bool on) {
  if (g_board.output_backend == OutputBackend::ShiftRegister) {
    const uint8_t bit = relay_bit(relay);
    if (bit == HPN_GPIO_UNASSIGNED) {
      return;
    }
    if (on) {
      g_sr_cache |= static_cast<uint8_t>(1u << bit);
    } else {
      g_sr_cache &= static_cast<uint8_t>(~(1u << bit));
    }
    write_shift_register();
    return;
  }

  const uint8_t pin = relay_pin(relay);
  if (is_assigned(pin)) {
    io::write_pin(pin, on ? kActuatorOnLevel : kActuatorOffLevel);
  }
}

void set_mixer_motor(bool enabled, bool open_direction) {
  if (g_board.output_backend == OutputBackend::ShiftRegister) {
    const uint8_t direction_bit = g_board.sr_bit_mix_direction;
    const uint8_t motion_bit = g_board.sr_bit_mix_motion;

    if (open_direction) {
      g_sr_cache |= static_cast<uint8_t>(1u << direction_bit);
    } else {
      g_sr_cache &= static_cast<uint8_t>(~(1u << direction_bit));
    }

    if (enabled) {
      g_sr_cache |= static_cast<uint8_t>(1u << motion_bit);
    } else {
      g_sr_cache &= static_cast<uint8_t>(~(1u << motion_bit));
    }

    write_shift_register();
    return;
  }

  // Direct-GPIO carriers use separate, hardware-interlocked open/close relays.
  const bool can_open = is_assigned(g_config.gpio_mix_open);
  const bool can_close = is_assigned(g_config.gpio_mix_close);
  if (can_open) {
    io::write_pin(g_config.gpio_mix_open,
                  (enabled && open_direction) ? kActuatorOnLevel : kActuatorOffLevel);
  }
  if (can_close) {
    io::write_pin(g_config.gpio_mix_close,
                  (enabled && !open_direction) ? kActuatorOnLevel : kActuatorOffLevel);
  }
}

void set_leds(const uint8_t* rgb, uint16_t count) {
  if (!g_leds_ready || rgb == nullptr) {
    return;
  }
  if (count > g_led_count) {
    count = g_led_count;
  }
  io::leds_show(rgb, count);
}

bool read_temperature(TempRole role, float& celsius) {
  return io::read_temperature(role, celsius);
}

float read_power_w(CurrentChannel channel) {
  return io::read_power_w(channel);
}

bool select_current_channel(CurrentChannel channel) {
  if (!g_board.has_current_mux || g_board.output_backend != OutputBackend::ShiftRegister) {
    return false;
  }

  const uint8_t index = static_cast<uint8_t>(channel);
  if (index >= kCurrentChannelCount) {
    return false;
  }

  const uint8_t mux_channel = g_board.current_mux_channel[index];
  if (mux_channel == 0xFF) {
    return false;
  }

  if (mux_channel & 0x01) {
    g_sr_cache |= static_cast<uint8_t>(1u << g_board.sr_bit_mux_s0);
  } else {
    g_sr_cache &= static_cast<uint8_t>(~(1u << g_board.sr_bit_mux_s0));
  }

  if (mux_channel & 0x02) {
    g_sr_cache |= static_cast<uint8_t>(1u << g_board.sr_bit_mux_s1);
  } else {
    g_sr_cache &= static_cast<uint8_t>(~(1u << g_board.sr_bit_mux_s1));
  }

  write_shift_register();
  io::delay_us(100);  // let the analogue switch settle
  return true;
}

uint16_t read_current_raw(CurrentChannel channel) {
  if (io::kEmulated) {
    // The emulation models power in watts, not an ADC waveform; use
    // read_power_w() instead.
    return 0;
  }
  if (!is_assigned(g_board.adc_pin)) {
    return 0;
  }
  if (!select_current_channel(channel)) {
    return 0;
  }
  return io::adc_read(g_board.adc_pin);
}

}  // namespace l0
}  // namespace hpn
