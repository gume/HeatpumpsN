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

bool pcf8574_begin(uint8_t sda_pin, uint8_t scl_pin, uint8_t address, uint8_t initial) {
  (void)sda_pin;
  (void)scl_pin;
  (void)address;
  sim::record_shift_register(initial);
  return true;
}

bool pcf8574_write(uint8_t value) {
  sim::record_shift_register(value);
  return true;
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

uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count) {
  return sim::discover_temperature_sensors(out, max_count);
}

}  // namespace io
}  // namespace hpn

#else  // !HPN_IO_EMULATION

#include <Adafruit_NeoPixel.h>
#include <Arduino.h>
#include <DallasTemperature.h>
#include <OneWire.h>
#include <Wire.h>

// -----------------------------------------------------------------------------
// Hardware backend: real pins, real shift register, real ADC and LED strip.
// -----------------------------------------------------------------------------

namespace hpn {
namespace io {
namespace {

Adafruit_NeoPixel g_leds;
bool g_leds_ready = false;
uint16_t g_led_count = 0;
uint8_t g_onewire_pin = HPN_GPIO_UNASSIGNED;
uint8_t g_pcf8574_address = 0x20;
bool g_pcf8574_ready = false;
const Config* g_config = nullptr;
OneWire* g_onewire = nullptr;
DallasTemperature* g_dallas = nullptr;
uint32_t g_dallas_request_ms = 0;
bool g_dallas_read_pending = false;
float g_dallas_c[3] = {};
bool g_dallas_valid[3] = {};

bool pin_assigned(uint8_t pin) { return pin != HPN_GPIO_UNASSIGNED; }

uint8_t role_index(TempRole role) {
  switch (role) {
    case TempRole::Boiler:
      return 0;
    case TempRole::ReturnFloor:
      return 1;
    case TempRole::MixOut:
      return 2;
    default:
      return 0xFF;
  }
}

const char* sensor_id_for_role(const Config& config, TempRole role) {
  switch (role) {
    case TempRole::Boiler:
      return config.sensor_id_boiler;
    case TempRole::ReturnFloor:
      return config.sensor_id_ret;
    case TempRole::MixOut:
      return config.sensor_id_mix;
    default:
      return "";
  }
}

uint8_t hex_nibble(char c) {
  if (c >= '0' && c <= '9') return static_cast<uint8_t>(c - '0');
  if (c >= 'a' && c <= 'f') return static_cast<uint8_t>(c - 'a' + 10);
  if (c >= 'A' && c <= 'F') return static_cast<uint8_t>(c - 'A' + 10);
  return 0xFF;
}

bool parse_hex_address(const char* hex, DeviceAddress out) {
  if (hex == nullptr || strlen(hex) < 16) {
    return false;
  }
  for (uint8_t i = 0; i < 8; ++i) {
    const uint8_t high = hex_nibble(hex[i * 2]);
    const uint8_t low = hex_nibble(hex[i * 2 + 1]);
    if (high == 0xFF || low == 0xFF) {
      return false;
    }
    out[i] = static_cast<uint8_t>((high << 4) | low);
  }
  return true;
}

void dallas_poll(uint32_t now_ms) {
  if (g_dallas == nullptr || g_config == nullptr) {
    return;
  }
  if (g_dallas_read_pending && now_ms - g_dallas_request_ms >= 750u) {
    const TempRole roles[] = {TempRole::Boiler, TempRole::ReturnFloor, TempRole::MixOut};
    for (uint8_t i = 0; i < 3; ++i) {
      DeviceAddress address{};
      if (!parse_hex_address(sensor_id_for_role(*g_config, roles[i]), address)) {
        g_dallas_valid[i] = false;
        continue;
      }
      const float celsius = g_dallas->getTempC(address);
      const bool valid = celsius > -100.0f && celsius < 150.0f;
      g_dallas_c[i] = celsius;
      g_dallas_valid[i] = valid;
    }
    g_dallas_read_pending = false;
  }
  if (now_ms - g_dallas_request_ms >= 1000u) {
    g_dallas->requestTemperatures();
    g_dallas_request_ms = now_ms;
    g_dallas_read_pending = true;
  }
}

}  // namespace

void begin(const Config& config, const BoardConfig& board) {
  (void)board;
  g_config = &config;
  g_onewire_pin = config.gpio_onewire;
  static OneWire onewire;
  static DallasTemperature dallas(&onewire);
  if (pin_assigned(g_onewire_pin)) {
    onewire.begin(g_onewire_pin);
    dallas.begin();
    dallas.setWaitForConversion(false);
    g_onewire = &onewire;
    g_dallas = &dallas;
    g_dallas_request_ms = 0;
    g_dallas_read_pending = false;
  }
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

bool pcf8574_begin(uint8_t sda_pin, uint8_t scl_pin, uint8_t address, uint8_t initial) {
  if (!pin_assigned(sda_pin) || !pin_assigned(scl_pin)) {
    g_pcf8574_ready = false;
    return false;
  }
  Wire.begin(sda_pin, scl_pin);
  Wire.setClock(100000);
  g_pcf8574_address = address;
  g_pcf8574_ready = true;
  // A PCF8574 has no register pointer: the first byte after the address is
  // the port value. Write the safe state before returning so the caller can
  // rely on the relays being off.
  Wire.beginTransmission(g_pcf8574_address);
  Wire.write(initial);
  const bool ok = Wire.endTransmission() == 0;
  g_pcf8574_ready = ok;
  return ok;
}

bool pcf8574_write(uint8_t value) {
  if (!g_pcf8574_ready) {
    return false;
  }
  Wire.beginTransmission(g_pcf8574_address);
  Wire.write(value);
  return Wire.endTransmission() == 0;
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
  const uint8_t index = role_index(role);
  if (index == 0xFF || g_dallas == nullptr || g_config == nullptr) {
    return false;
  }
  dallas_poll(millis());
  if (g_dallas_valid[index]) {
    celsius = g_dallas_c[index];
    return true;
  }
  return false;
}

float read_power_w(CurrentChannel channel) {
  // TODO(L0): convert the MUX/ADC sample with the carrier calibration
  // (firmware/docs/hhn-1.0.md §4.1). Until then only the emulation reports
  // power.
  (void)channel;
  return 0.0f;
}

uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count) {
  if (out == nullptr || max_count == 0 || !pin_assigned(g_onewire_pin)) {
    return 0;
  }

  OneWire wire(g_onewire_pin);
  DallasTemperature sensors(&wire);
  sensors.begin();
  sensors.setWaitForConversion(false);
  sensors.requestTemperatures();
  // A 12-bit DS18B20 conversion takes up to 750 ms. This path is only used by
  // the explicit discovery button, never by the control loop.
  delay(750);

  uint8_t count = 0;
  DeviceAddress address{};
  while (count < max_count && sensors.getAddress(address, count)) {
    snprintf(out[count].id, sizeof(out[count].id),
             "%02x%02x%02x%02x%02x%02x%02x%02x", address[0], address[1], address[2],
             address[3], address[4], address[5], address[6], address[7]);
    const float celsius = sensors.getTempC(address);
    out[count].valid = celsius > -100.0f && celsius < 150.0f;
    out[count].celsius = out[count].valid ? celsius : NAN;
    ++count;
  }
  return count;
}

}  // namespace io
}  // namespace hpn

#endif  // HPN_IO_EMULATION
