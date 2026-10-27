#pragma once

#include <stdint.h>

#include "hpn/config.h"
#include "hpn/io_types.h"

// Board-level wiring abstraction.
//
// The controller logic is board-agnostic. A BoardConfig describes the fixed
// parts of a carrier: its name, the analogue current-measurement front end and
// the MUX channel map. The output driver (direct GPIO, 74HC595 or PCF8574) is
// runtime configuration, because the same carrier can be fitted with different
// extender boards. See firmware/docs/hhn-1.0.md.
//
// Carriers:
//
//   HomeNet Heatpumps 1.0   legacy carrier, currently a LOLIN S2 Mini inside.
//   HomeNet Heatpumps 2.0   next carrier, under planning. Primary MCUs are
//                           ESP32-C3 and ESP32-S3. No profile yet.
//   boardless               bare devkit, no carrier. Direct GPIO with all
//                           assignments still unset.
//
// The carrier profile is independent of the I/O backend: the same profile can
// be run against real pins or against the emulation model (HPN_IO_EMULATION).
namespace hpn {

struct BoardConfig {
  const char* name;

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
