#pragma once

#include <stdint.h>

#include "hpn/config.h"

// Board-level wiring abstraction.
//
// The controller logic is board-agnostic. A BoardConfig describes how the
// current PCB reaches the actuators and sensors. The temporary board is the
// original LOLIN S2 Mini carrier, documented in firmware/docs/board-old-lolin-s2.md.
namespace hpn {

enum class OutputBackend : uint8_t {
  DirectGpio = 0,
  ShiftRegister = 1,  // 74HC595, one byte
};

enum class CurrentChannel : uint8_t {
  PumpFloor = 0,
  PumpRad1 = 1,
  PumpRad2 = 2,
  Mixer = 3,
  Count = 4,
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
  uint8_t current_mux_channel[4];  // indexed by CurrentChannel; 0xFF = absent
};

// Active board profile, selected by HPN_BOARD_* in platformio.ini.
const BoardConfig& board_config();

// Apply board-specific values to the generic configuration (pins, sensor ROM
// IDs, LED count, mixer travel time, ...).
void board_apply_defaults(Config& config);

}  // namespace hpn
