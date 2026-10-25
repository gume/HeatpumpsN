#include "hpn/l0_io.h"

#include <Adafruit_NeoPixel.h>
#include <Arduino.h>

#include "hpn/build_target.h"

namespace hpn {
namespace l0 {
namespace {

// TODO(hardware): confirm the relay-driver polarity on the old carrier.
// The 74HC595 drives the relay board; active-high is assumed.
constexpr uint8_t kActuatorOnLevel = HIGH;
constexpr uint8_t kActuatorOffLevel = LOW;

Config g_config{};
BoardConfig g_board{};
uint8_t g_sr_cache = 0;
Adafruit_NeoPixel g_leds;
bool g_leds_ready = false;

bool is_assigned(uint8_t pin) { return pin != HPN_GPIO_UNASSIGNED; }

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
  }
  return HPN_GPIO_UNASSIGNED;
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
  }
  return HPN_GPIO_UNASSIGNED;
}

void init_output_pin(uint8_t pin, const char* name) {
  if (!is_assigned(pin)) {
    Serial.printf("[l0] %-11s unassigned\n", name);
    return;
  }

  digitalWrite(pin, kActuatorOffLevel);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, kActuatorOffLevel);
  Serial.printf("[l0] %-11s GPIO%u (safe=OFF)\n", name, static_cast<unsigned>(pin));
}

// A 74HC595 with MSBFIRST puts bit 0 on Q0 (QA), matching the old ESPHome
// configuration.
void write_shift_register() {
  if (g_board.output_backend != OutputBackend::ShiftRegister) {
    return;
  }

  digitalWrite(g_board.sr_latch_pin, LOW);
  shiftOut(g_board.sr_data_pin, g_board.sr_clock_pin, MSBFIRST, g_sr_cache);
  digitalWrite(g_board.sr_latch_pin, HIGH);
  digitalWrite(g_board.sr_latch_pin, LOW);
}

void init_shift_register() {
  if (!is_assigned(g_board.sr_data_pin) || !is_assigned(g_board.sr_clock_pin) ||
      !is_assigned(g_board.sr_latch_pin)) {
    Serial.println("[l0] shift-register pins missing; outputs stay disabled");
    return;
  }

  digitalWrite(g_board.sr_data_pin, LOW);
  pinMode(g_board.sr_data_pin, OUTPUT);
  digitalWrite(g_board.sr_clock_pin, LOW);
  pinMode(g_board.sr_clock_pin, OUTPUT);
  digitalWrite(g_board.sr_latch_pin, LOW);
  pinMode(g_board.sr_latch_pin, OUTPUT);

  if (is_assigned(g_board.sr_oe_pin)) {
    const uint8_t disable_level = g_board.sr_oe_active_low ? HIGH : LOW;
    digitalWrite(g_board.sr_oe_pin, disable_level);
    pinMode(g_board.sr_oe_pin, OUTPUT);
    digitalWrite(g_board.sr_oe_pin, disable_level);
  }

  // Zero every output before enabling the relay drivers.
  g_sr_cache = 0;
  write_shift_register();

  if (is_assigned(g_board.sr_oe_pin)) {
    const uint8_t enable_level = g_board.sr_oe_active_low ? LOW : HIGH;
    digitalWrite(g_board.sr_oe_pin, enable_level);
  }

  Serial.printf("[l0] 74HC595 data=%u clock=%u latch=%u oe=%u\n",
                static_cast<unsigned>(g_board.sr_data_pin),
                static_cast<unsigned>(g_board.sr_clock_pin),
                static_cast<unsigned>(g_board.sr_latch_pin),
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
    pinMode(g_board.adc_pin, INPUT);
  }

  // 74HC4051 S2/S3 are held low on the old carrier; only channels 0-3 are used.
  if (is_assigned(g_board.mux_s2_pin)) {
    pinMode(g_board.mux_s2_pin, OUTPUT);
    digitalWrite(g_board.mux_s2_pin, LOW);
  }
  if (g_board.mux_s3_pin != g_board.mux_s2_pin && is_assigned(g_board.mux_s3_pin)) {
    pinMode(g_board.mux_s3_pin, OUTPUT);
    digitalWrite(g_board.mux_s3_pin, LOW);
  }

  analogReadResolution(12);
  Serial.printf("[l0] current MUX adc=GPIO%u\n", static_cast<unsigned>(g_board.adc_pin));
}

void init_leds() {
  if (!is_assigned(g_config.gpio_led_data) || g_config.num_leds == 0) {
    return;
  }

  g_leds.updateLength(g_config.num_leds);
  g_leds.setPin(static_cast<int16_t>(g_config.gpio_led_data));
  g_leds.begin();
  g_leds.clear();
  g_leds.show();
  g_leds_ready = true;

  Serial.printf("[l0] LEDs      GPIO%u x%u\n", static_cast<unsigned>(g_config.gpio_led_data),
                static_cast<unsigned>(g_config.num_leds));
}

void leds_off() {
  if (g_leds_ready) {
    g_leds.clear();
    g_leds.show();
  }
}

}  // namespace

void begin(const Config& config, const BoardConfig& board) {
  g_config = config;
  g_board = board;
  g_sr_cache = 0;
  g_leds_ready = false;

  Serial.printf("[l0] board     %s (%s)\n", g_board.name,
                g_board.output_backend == OutputBackend::ShiftRegister ? "74HC595"
                                                                       : "direct GPIO");

  if (g_board.output_backend == OutputBackend::ShiftRegister) {
    init_shift_register();
  } else {
    init_direct_outputs();
  }

  init_current_mux();
  init_leds();
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
        digitalWrite(pin, kActuatorOffLevel);
      }
    }
  }

  leds_off();
}

void tick(const Config& config, const BoardConfig& board, SystemState& state) {
  // Placeholder for the L0 pulse state machine, 1-Wire conversion scheduling,
  // encoder sampling, and pump-power plausibility checks.
  (void)config;
  (void)board;
  (void)state;
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
    digitalWrite(pin, on ? kActuatorOnLevel : kActuatorOffLevel);
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

  // Direct-GPIO boards use separate, hardware-interlocked open/close relays.
  const bool can_open = is_assigned(g_config.gpio_mix_open);
  const bool can_close = is_assigned(g_config.gpio_mix_close);
  if (can_open) {
    digitalWrite(g_config.gpio_mix_open,
                (enabled && open_direction) ? kActuatorOnLevel : kActuatorOffLevel);
  }
  if (can_close) {
    digitalWrite(g_config.gpio_mix_close,
                (enabled && !open_direction) ? kActuatorOnLevel : kActuatorOffLevel);
  }
}

bool select_current_channel(CurrentChannel channel) {
  if (!g_board.has_current_mux ||
      g_board.output_backend != OutputBackend::ShiftRegister) {
    return false;
  }

  const uint8_t index = static_cast<uint8_t>(channel);
  if (index >= static_cast<uint8_t>(CurrentChannel::Count)) {
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
  delayMicroseconds(100);  // let the analogue switch settle
  return true;
}

uint16_t read_current_raw(CurrentChannel channel) {
  if (!is_assigned(g_board.adc_pin)) {
    return 0;
  }
  if (!select_current_channel(channel)) {
    return 0;
  }
  return analogRead(g_board.adc_pin);
}

}  // namespace l0
}  // namespace hpn
