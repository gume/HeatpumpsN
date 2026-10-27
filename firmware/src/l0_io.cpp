#include "hpn/l0_io.h"

#include <Arduino.h>
#include <math.h>
#include <time.h>

#include "hpn/io_backend.h"

namespace hpn {
namespace l0 {
namespace {

// The output driver is runtime configuration. The legacy carrier uses a
// 74HC595-compatible serial extender whose selected outputs pull the
// relay-driver inputs to GND at boot/reset (active-high relay board). The next
// carrier uses a PCF8574 whose pins idle high and whose relay board is
// active-low, so its safe byte is 0xFF. Both cases are handled by
// `output_active_low`; the MUX select bits always stay active-high.
Config g_config{};
BoardConfig g_board{};
uint8_t g_output_byte = 0;
bool g_pcf_write_error_logged = false;
uint16_t g_led_count = 0;
bool g_leds_ready = false;
uint32_t g_last_temp_poll_ms = 0;
uint32_t g_last_power_poll_ms = 0;

bool is_assigned(uint8_t pin) {
  return pin != HPN_GPIO_UNASSIGNED;
}

bool uses_expander() {
  return g_config.output_expander == OUTPUT_EXPANDER_74HC595 ||
         g_config.output_expander == OUTPUT_EXPANDER_PCF8574;
}

bool uses_shift_register() {
  return g_config.output_expander == OUTPUT_EXPANDER_74HC595;
}

uint8_t output_level(bool on) {
  return g_config.output_active_low ? (on ? io::kLow : io::kHigh)
                                    : (on ? io::kHigh : io::kLow);
}

uint8_t with_bit(uint8_t byte, uint8_t bit, bool high) {
  if (bit > 7) {
    return byte;
  }
  if (high) {
    return static_cast<uint8_t>(byte | (1u << bit));
  }
  return static_cast<uint8_t>(byte & ~(1u << bit));
}

// Set an actuator bit in the raw output byte. The MUX helpers use with_bit()
// directly because those select lines are not affected by output polarity.
uint8_t with_output(uint8_t byte, uint8_t bit, bool on) {
  return with_bit(byte, bit, output_level(on) != io::kLow);
}

uint8_t inactive_output_byte() {
  uint8_t byte = g_config.output_active_low ? 0xFF : 0x00;
  byte = with_bit(byte, g_config.expander_bit_mux_s0, false);
  byte = with_bit(byte, g_config.expander_bit_mux_s1, false);
  return byte;
}

uint8_t relay_bit(Relay relay) {
  switch (relay) {
    case Relay::Heater:
      return g_config.expander_bit_heater;
    case Relay::PumpRad1:
      return g_config.expander_bit_pump_rad1;
    case Relay::PumpRad2:
      return g_config.expander_bit_pump_rad2;
    case Relay::PumpFloor:
      return g_config.expander_bit_pump_floor;
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

  const uint8_t off = output_level(false);
  io::write_pin(pin, off);
  io::pin_mode(pin, io::kModeOutput);
  io::write_pin(pin, off);
  Serial.printf("[l0] %-11s GPIO%u (safe=OFF%s)\n", name, static_cast<unsigned>(pin),
                g_config.output_active_low ? ", active-low" : "");
}

// A shift register with MSBFIRST puts bit 0 on Q0 (QA), matching the legacy
// ESPHome configuration.
void write_outputs() {
  if (uses_shift_register()) {
    if (!is_assigned(g_config.expander_data_pin) || !is_assigned(g_config.expander_clock_pin) ||
        !is_assigned(g_config.expander_latch_pin)) {
      return;
    }
    io::shift_out_msb(g_config.expander_data_pin, g_config.expander_clock_pin,
                      g_config.expander_latch_pin, g_output_byte);
  } else if (g_config.output_expander == OUTPUT_EXPANDER_PCF8574) {
    if (!io::pcf8574_write(g_output_byte) && !g_pcf_write_error_logged) {
      Serial.printf("[l0] PCF8574 write failed (addr 0x%02X)\n",
                    static_cast<unsigned>(g_config.expander_address));
      g_pcf_write_error_logged = true;
    }
  }
}

void init_shift_register() {
  if (!is_assigned(g_config.expander_data_pin) || !is_assigned(g_config.expander_clock_pin) ||
      !is_assigned(g_config.expander_latch_pin)) {
    Serial.println("[l0] shift-register pins missing; outputs stay disabled");
    return;
  }

  io::write_pin(g_config.expander_data_pin, io::kLow);
  io::pin_mode(g_config.expander_data_pin, io::kModeOutput);
  io::write_pin(g_config.expander_clock_pin, io::kLow);
  io::pin_mode(g_config.expander_clock_pin, io::kModeOutput);
  io::write_pin(g_config.expander_latch_pin, io::kLow);
  io::pin_mode(g_config.expander_latch_pin, io::kModeOutput);

  if (is_assigned(g_config.expander_oe_pin)) {
    const uint8_t disable_level =
        g_config.expander_oe_active_low ? io::kHigh : io::kLow;
    io::write_pin(g_config.expander_oe_pin, disable_level);
    io::pin_mode(g_config.expander_oe_pin, io::kModeOutput);
    io::write_pin(g_config.expander_oe_pin, disable_level);
  }

  // Zero every output before enabling the relay drivers. The inactive byte is
  // 0x00 for active-high outputs and 0xFF for active-low outputs.
  g_output_byte = inactive_output_byte();
  write_outputs();

  if (is_assigned(g_config.expander_oe_pin)) {
    const uint8_t enable_level =
        g_config.expander_oe_active_low ? io::kLow : io::kHigh;
    io::write_pin(g_config.expander_oe_pin, enable_level);
  }

  Serial.printf(
      "[l0] 74HC595   data=%u clock=%u latch=%u oe=%u (%s)\n",
      static_cast<unsigned>(g_config.expander_data_pin),
      static_cast<unsigned>(g_config.expander_clock_pin),
      static_cast<unsigned>(g_config.expander_latch_pin),
      static_cast<unsigned>(g_config.expander_oe_pin),
      g_config.expander_oe_active_low ? "OE active-low" : "OE active-high");
}

void init_pcf8574() {
  g_output_byte = inactive_output_byte();
  const bool ok = io::pcf8574_begin(g_config.i2c_sda, g_config.i2c_scl,
                                    g_config.expander_address, g_output_byte);
  Serial.printf("[l0] PCF8574   addr=0x%02X sda=%u scl=%u (%s)\n",
                static_cast<unsigned>(g_config.expander_address),
                static_cast<unsigned>(g_config.i2c_sda),
                static_cast<unsigned>(g_config.i2c_scl),
                ok ? "safe state written" : "NOT ACKNOWLEDGED");
  g_pcf_write_error_logged = !ok;
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
  g_output_byte = 0;
  g_pcf_write_error_logged = false;
  g_leds_ready = false;
  g_led_count = 0;
  g_last_temp_poll_ms = 0;
  g_last_power_poll_ms = 0;

  const char* driver = "direct GPIO";
  if (g_config.output_expander == OUTPUT_EXPANDER_74HC595) {
    driver = "74HC595 output extender";
  } else if (g_config.output_expander == OUTPUT_EXPANDER_PCF8574) {
    driver = "PCF8574 output extender";
  }
  Serial.printf("[l0] carrier   %s (%s)\n", g_board.name, driver);
  Serial.printf("[l0] backend   %s\n", io::kEmulated ? "emulation" : "hardware");

  // Seed the emulation model (or the hardware backend) with this carrier.
  io::begin(g_config, g_board);

  if (g_config.output_expander == OUTPUT_EXPANDER_74HC595) {
    init_shift_register();
  } else if (g_config.output_expander == OUTPUT_EXPANDER_PCF8574) {
    init_pcf8574();
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
  if (uses_expander()) {
    // All relays off, mixer motion off, and MUX channel 0 selected. For an
    // active-low relay board every actuator bit is driven high.
    g_output_byte = inactive_output_byte();
    write_outputs();
  } else {
    const uint8_t off = output_level(false);
    const uint8_t pins[] = {
        g_config.gpio_pump_floor, g_config.gpio_pump_rad1, g_config.gpio_pump_rad2,
        g_config.gpio_heater,     g_config.gpio_mix_open,  g_config.gpio_mix_close,
    };
    for (uint8_t pin : pins) {
      if (is_assigned(pin)) {
        io::write_pin(pin, off);
      }
    }
  }

  leds_off();
}

void tick(const Config& config, const BoardConfig& board, SystemState& state) {
  const uint32_t now = millis();

  // Publish the synchronized wall clock once SNTP has a valid epoch. Keeping
  // this in L0 avoids pulling <time.h> into every layer that reads the state.
  const time_t epoch = time(nullptr);
  if (epoch > 1600000000) {
    state.wall_time_epoch_s = static_cast<uint64_t>(epoch);
  }

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
        const float previous = reading.value_c;
        const uint32_t previous_ms = reading.last_valid_ms;
        if (reading.is_valid && previous_ms != 0 && now > previous_ms) {
          const float dt_s = static_cast<float>(now - previous_ms) / 1000.0f;
          reading.gradient_k_per_s = (celsius - previous) / dt_s;
        } else {
          reading.gradient_k_per_s = 0.0f;
        }
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
    state.pump_power_w[0] = read_power_w(config, CurrentChannel::PumpFloor);
    state.pump_power_w[1] = read_power_w(config, CurrentChannel::PumpRad1);
    state.pump_power_w[2] = read_power_w(config, CurrentChannel::PumpRad2);
    state.mixer_power_w = read_power_w(config, CurrentChannel::Mixer);
  }

  // Encoder sampling and the remaining L0 diagnostics are still TODO(L0). The
  // mixer pulse state machine now lives in L1 so bench and hardware builds
  // share the same actuator path.
  (void)board;
}

void set_relay(Relay relay, bool on) {
  if (uses_expander()) {
    const uint8_t bit = relay_bit(relay);
    if (bit > 7) {
      return;
    }
    g_output_byte = with_output(g_output_byte, bit, on);
    write_outputs();
    return;
  }

  const uint8_t pin = relay_pin(relay);
  if (is_assigned(pin)) {
    io::write_pin(pin, output_level(on));
  }
}

void set_mixer_motor(bool enabled, bool open_direction) {
  const bool open_close = g_config.mixer_wiring == MIX_WIRING_OPEN_CLOSE;

  if (uses_expander()) {
    // The two carrier channels are logical open/direction and close/motion.
    // Legacy HHN 1.0 uses direction + motion enable; the new carrier uses two
    // interlocked open/close relays. Reuse the same two bits for both modes.
    const uint8_t open_or_direction_bit = g_config.expander_bit_mix_direction;
    const uint8_t close_or_motion_bit = g_config.expander_bit_mix_motion;
    const bool first_on = open_close ? (enabled && open_direction) : open_direction;
    const bool second_on = open_close ? (enabled && !open_direction) : enabled;

    g_output_byte = with_output(g_output_byte, open_or_direction_bit, first_on);
    g_output_byte = with_output(g_output_byte, close_or_motion_bit, second_on);
    write_outputs();
    return;
  }

  const bool can_open = is_assigned(g_config.gpio_mix_open);
  const bool can_close = is_assigned(g_config.gpio_mix_close);
  if (open_close) {
    // Separate, hardware-interlocked open/close relays.
    if (can_open) {
      io::write_pin(g_config.gpio_mix_open, output_level(enabled && open_direction));
    }
    if (can_close) {
      io::write_pin(g_config.gpio_mix_close, output_level(enabled && !open_direction));
    }
    return;
  }

  // Legacy direction + motion: first channel is direction, second is enable.
  if (can_open) {
    io::write_pin(g_config.gpio_mix_open, output_level(open_direction));
  }
  if (can_close) {
    io::write_pin(g_config.gpio_mix_close, output_level(enabled));
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

float read_power_w(const Config& config, CurrentChannel channel) {
  if (io::kEmulated) {
    return io::read_power_w(channel);
  }
  if (!g_board.has_current_mux || !is_assigned(g_board.adc_pin)) {
    return 0.0f;
  }
  if (!select_current_channel(channel)) {
    return 0.0f;
  }

  // ACS712/CT front end: sample two mains cycles and remove the DC bias.
  // The shared calibration comes from Settings -> Hardware -> Pump current.
  // The default is the legacy ESPHome ct_clamp fit (0.004 -> 0 W,
  // 0.072 -> 40 W), approximately 588 W/V above the zero point.
  const uint32_t start = micros();
  uint32_t count = 0;
  float mean = 0.0f;
  float m2 = 0.0f;
  while (micros() - start < 40000u) {
    const float volts =
        static_cast<float>(io::adc_read(g_board.adc_pin)) * (3.3f / 4095.0f);
    ++count;
    const float delta = volts - mean;
    mean += delta / static_cast<float>(count);
    m2 += delta * (volts - mean);
  }
  if (count < 2) {
    return 0.0f;
  }
  const float rms_volts = sqrtf(m2 / static_cast<float>(count));
  float offset_v = config.pump_current_offset_v;
  float w_per_v = config.pump_current_w_per_v;
  if (!isfinite(offset_v) || offset_v < 0.0f || offset_v > 1.0f) offset_v = 0.004f;
  if (!isfinite(w_per_v) || w_per_v < 1.0f || w_per_v > 5000.0f) w_per_v = 588.2f;
  float watts = (rms_volts - offset_v) * w_per_v;
  if (watts < 5.0f) {
    watts = 0.0f;
  } else if (watts > 2000.0f) {
    watts = 2000.0f;
  }
  return watts;
}

uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count) {
  return io::discover_temperature_sensors(out, max_count);
}

bool select_current_channel(CurrentChannel channel) {
  if (!g_board.has_current_mux || !uses_expander()) {
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

  // MUX select lines are active-high regardless of the relay polarity.
  g_output_byte = with_bit(g_output_byte, g_config.expander_bit_mux_s0,
                           (mux_channel & 0x01) != 0);
  g_output_byte = with_bit(g_output_byte, g_config.expander_bit_mux_s1,
                           (mux_channel & 0x02) != 0);

  write_outputs();
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
