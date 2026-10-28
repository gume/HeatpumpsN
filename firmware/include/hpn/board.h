#pragma once

#include <stdint.h>

#include "hpn/config.h"
#include "hpn/io_types.h"

// Board-level wiring abstraction.
//
// The controller logic is board-agnostic. A BoardConfig describes the fixed
// parts of a hardware board: its name, the analogue current-measurement front
// end and its channel map. The output driver (direct GPIO, 74HC595 or
// PCF8574) is runtime configuration, because boards can be re-fitted or
// extended. See firmware/docs/boards.md.
//
// Boards:
//
//   HomeNet Heating control v0.1   legacy board with an ESP32-S2 (LOLIN S2
//                                  Mini) and a 74HC595 output extender.
//   HomeNet Heating control v1.x   current board with an ESP32-C3 or ESP32-S3,
//                                  a PCF8574 expander, a fault input and an
//                                  ADS1115 current front end.
//   boardless                      bare devkit, no board. Direct GPIO with all
//                                  assignments still unset.
//
// The board profile is independent of the I/O backend: the same profile can
// be run against real pins or against the emulation model (HPN_IO_EMULATION).
// The MCU only supplies capabilities (core count, pin numbers); it never
// enables or removes a feature. Anything a board offers is described here.
namespace hpn {

struct BoardConfig {
  const char* name;

  // Current-measurement front end (fixed by the board).
  bool has_current_mux;  // v0.1: 74HC4051 into one ADC pin
  uint8_t adc_pin;
  uint8_t mux_s2_pin;
  uint8_t mux_s3_pin;
  uint8_t current_mux_channel[kCurrentChannelCount];  // 0xFF = absent
  // v1.x: ADS1115 single-ended input per logical channel (0-3, 0xFF = absent).
  uint8_t current_ads_channel[kCurrentChannelCount];
};

// Active carrier profile, selected by HPN_CARRIER_* in platformio.ini.
const BoardConfig& board_config();

// Apply carrier-specific values to the generic configuration (pins, sensor ROM
// IDs, LED count, mixer travel time, ...).
void board_apply_defaults(Config& config);

}  // namespace hpn
