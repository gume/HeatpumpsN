#include "hpn/io_backend.h"

#if defined(HPN_IO_EMULATION)

#include "hpn/sim.h"

// -----------------------------------------------------------------------------
// Emulated backend: no pin is touched. L0 drives the simulation model, and the
// mocked sensor answers come from hpn::sim.
// -----------------------------------------------------------------------------

namespace hpn {
namespace io {

void begin(const Config& config, const BoardConfig& board) {
  sim::begin(config, board);
}

void pin_mode(uint8_t pin, uint8_t mode) {
  (void)pin;
  (void)mode;
}

void write_pin(uint8_t pin, uint8_t level) {
  sim::record_pin(pin, level != kLow);
}

void shift_out_msb(uint8_t data_pin, uint8_t clock_pin, uint8_t latch_pin, uint8_t value) {
  (void)data_pin;
  (void)clock_pin;
  (void)latch_pin;
  sim::record_shift_register(value);
}

void adc_begin(uint8_t pin) {
  (void)pin;
}

void adc_set_resolution(uint8_t bits) {
  (void)bits;
}

uint16_t adc_read(uint8_t pin) {
  (void)pin;
  // No waveform model: use l0::read_power_w() for emulated current.
  return 0;
}

void leds_begin(uint8_t pin, uint16_t count) {
  (void)pin;
  sim::record_led_count(count);
}

void leds_show(const uint8_t* rgb, uint16_t count) {
  sim::record_led_frame(rgb, count);
}

void delay_us(uint32_t us) {
  (void)us;
}

bool read_temperature(TempRole role, float& celsius) {
  return sim::read_temperature(role, celsius);
}

float read_power_w(CurrentChannel channel) {
  return sim::read_power_w(channel);
}

}  // namespace io
}  // namespace hpn

#else  // !HPN_IO_EMULATION

#include <Adafruit_NeoPixel.h>
#include <Arduino.h>

// -----------------------------------------------------------------------------
// Hardware backend: real pins, real shift register, real ADC and LED strip.
// -----------------------------------------------------------------------------

namespace hpn {
namespace io {
namespace {

Adafruit_NeoPixel g_leds;
bool g_leds_ready = false;
uint16_t g_led_count = 0;

}  // namespace

void begin(const Config& config, const BoardConfig& board) {
  (void)config;
  (void)board;
}

void pin_mode(uint8_t pin, uint8_t mode) {
  pinMode(pin, mode == kModeOutput ? OUTPUT : INPUT);
}

void write_pin(uint8_t pin, uint8_t level) {
  digitalWrite(pin, level == kHigh ? HIGH : LOW);
}

void shift_out_msb(uint8_t data_pin, uint8_t clock_pin, uint8_t latch_pin, uint8_t value) {
  digitalWrite(latch_pin, LOW);
  shiftOut(data_pin, clock_pin, MSBFIRST, value);
  digitalWrite(latch_pin, HIGH);
  digitalWrite(latch_pin, LOW);
}

void adc_begin(uint8_t pin) {
  pinMode(pin, INPUT);
}

void adc_set_resolution(uint8_t bits) {
  analogReadResolution(bits);
}

uint16_t adc_read(uint8_t pin) {
  return static_cast<uint16_t>(analogRead(pin));
}

void leds_begin(uint8_t pin, uint16_t count) {
  g_led_count = count;
  g_leds.updateLength(count);
  g_leds.setPin(static_cast<int16_t>(pin));
  g_leds.begin();
  g_leds.clear();
  g_leds.show();
  g_leds_ready = true;
}

void leds_show(const uint8_t* rgb, uint16_t count) {
  if (!g_leds_ready || rgb == nullptr) {
    return;
  }
  if (count > g_led_count) {
    count = g_led_count;
  }

  for (uint16_t i = 0; i < count; ++i) {
    g_leds.setPixelColor(i, rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]);
  }
  g_leds.show();
}

void delay_us(uint32_t us) {
  delayMicroseconds(us);
}

bool read_temperature(TempRole role, float& celsius) {
  // TODO(L0): 1-Wire broadcast convert + MATCH ROM reads (plan 02 §2.4).
  (void)role;
  (void)celsius;
  return false;
}

float read_power_w(CurrentChannel channel) {
  // TODO(L0): convert the MUX/ADC sample with the carrier calibration
  // (firmware/docs/hhn-1.0.md §4.1). Until then only the emulation reports
  // power.
  (void)channel;
  return 0.0f;
}

}  // namespace io
}  // namespace hpn

#endif  // HPN_IO_EMULATION
