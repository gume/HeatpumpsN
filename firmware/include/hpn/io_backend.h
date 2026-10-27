#pragma once

#include <stdint.h>

#include "hpn/board.h"
#include "hpn/config.h"
#include "hpn/io_types.h"

// Physical I/O backend.
//
// L0 calls these helpers instead of Arduino pin functions. The implementation
// is selected at compile time:
//
//   default                  real pins, shift register, ADC and LED strip
//   -DHPN_IO_EMULATION       in-memory model of the active carrier (no pins)
//
// The carrier profile still decides the wiring (bits, MUX channels, pin
// numbers), so the same L0 code runs against real hardware and against the
// emulation. See firmware/docs/io-emulation.md.
namespace hpn {
namespace io {

#if defined(HPN_IO_EMULATION)
constexpr bool kEmulated = true;
#else
constexpr bool kEmulated = false;
#endif

constexpr uint8_t kLow = 0;
constexpr uint8_t kHigh = 1;
constexpr uint8_t kModeInput = 0;
constexpr uint8_t kModeOutput = 1;

// Initialize the backend for the active carrier. In emulation mode this seeds
// the simulation model with the carrier wiring.
void begin(const Config& config, const BoardConfig& board);

void pin_mode(uint8_t pin, uint8_t mode);
void write_pin(uint8_t pin, uint8_t level);

// Shift one byte into a 74HC595 and pulse the latch.
void shift_out_msb(uint8_t data_pin, uint8_t clock_pin, uint8_t latch_pin, uint8_t value);

// Start the I2C bus for a PCF8574-style port expander and write an initial
// output byte. Returns false when the bus/address did not acknowledge.
bool pcf8574_begin(uint8_t sda_pin, uint8_t scl_pin, uint8_t address, uint8_t initial);
// Write the expander port. Returns false when the transfer failed.
bool pcf8574_write(uint8_t value);

void adc_begin(uint8_t pin);
void adc_set_resolution(uint8_t bits);
uint16_t adc_read(uint8_t pin);

void leds_begin(uint8_t pin, uint16_t count);
// rgb: count * 3 bytes, logical RGB order (the chipset order is a backend
// detail).
void leds_show(const uint8_t* rgb, uint16_t count);

void delay_us(uint32_t us);

// Mocked in emulation, not implemented on hardware yet.
bool read_temperature(TempRole role, float& celsius);
float read_power_w(CurrentChannel channel);

// Enumerate temperature sensors on the active carrier bus. On hardware this
// performs a blocking 1-Wire conversion (~750 ms); only call it from an
// explicit user action.
uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count);

}  // namespace io
}  // namespace hpn
