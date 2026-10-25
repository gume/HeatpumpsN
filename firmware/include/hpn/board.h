#pragma once

#include <stdint.h>

#include "hpn/config.h"
#include "hpn/io_types.h"

// Board-level wiring abstraction.
//
// The controller logic is board-agnostic. A BoardConfig describes how the
// active carrier reaches the actuators and sensors. Carriers:
//
//   HomeNet Heatpumps 1.0   legacy carrier, currently a LOLIN S2 Mini inside.
//                           See firmware/docs/hhn-1.0.md.
//   HomeNet Heatpumps 2.0   next carrier, under planning. Primary MCUs are
//                           ESP32-C3 and ESP32-S3. No profile yet.
//   boardless               bare devkit, no carrier. Direct GPIO with all
//                           assignments still unset.
//
// The carrier profile is independent of the I/O backend: the same profile can
// be run against real pins or against the emulation model (HPN_IO_EMULATION).
namespace hpn {

enum class OutputBackend : uint8_t {
  DirectGpio = 0,
  ShiftRegister = 1,  // 74HC595, one byte
};

struct BoardConfig {
  const char* name;
  OutputBackend output_backend;

  // 74HC595 pins (ignored for DirectGpio)
  uint8_t sr_data_pin;
  uint8_t sr_clock_pin;
  uint8_t sr_latch_pin;
  uint8_t sr_oe_pin;
  bool sr_oe_active_low;

  // 74HC595 output bit assignment (Q0..Q7)
  uint8_t sr_bit_heater;
  uint8_t sr_bit_pump_rad1;
  uint8_t sr_bit_pump_rad2;
  uint8_t sr_bit_pump_floor;
  uint8_t sr_bit_mix_direction;  // 1 = open, 0 = close
  uint8_t sr_bit_mix_motion;     // 1 = motor energised
  uint8_t sr_bit_mux_s0;
  uint8_t sr_bit_mux_s1;

  // Analogue current-measurement front end (ACS712 + mux)
  bool has_current_mux;
  uint8_t adc_pin;
  uint8_t mux_s2_pin;
  uint8_t mux_s3_pin;
  uint8_t current_mux_channel[kCurrentChannelCount];  // 0xFF = absent
};

// Active carrier profile, selected by HPN_CARRIER_* in platformio.ini.
const BoardConfig& board_config();

// Apply carrier-specific values to the generic configuration (pins, sensor ROM
// IDs, LED count, mixer travel time, ...).
void board_apply_defaults(Config& config);

}  // namespace hpn
