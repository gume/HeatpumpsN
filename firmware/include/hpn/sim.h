#pragma once

#include <stdint.h>

#include "hpn/board.h"
#include "hpn/config.h"
#include "hpn/io_types.h"

// Emulation model of a HomeNet Heatpumps carrier.
//
// Only compiled into the firmware with -DHPN_IO_EMULATION. L0 writes its
// outputs into this model instead of physical pins, and reads mocked sensor
// answers from it. The future web dashboard reads `snapshot()` and calls the
// setters to mock temperatures and pump power.
//
// Deliberately simple: no 1-Wire bus timing, no ADC waveform, no relay
// physics. It models *answers*, not electronics.
namespace hpn {
namespace sim {

struct Snapshot {
  uint32_t revision;

  // Mocked sensor answers (web UI writes these).
  float temperature_c[kTempRoleCount];
  bool temperature_valid[kTempRoleCount];
  float power_w[kCurrentChannelCount];

  // Observed outputs (L0 writes these through the I/O backend).
  bool relay_on[kRelayCount];
  bool mixer_motion;
  bool mixer_open;
  uint8_t mux_channel;  // 0xFF when the carrier has no current MUX
  uint8_t output_byte;  // last 74HC595 latch value (0 for direct-GPIO carriers)
  bool output_enabled;  // 74HC595 OE state
  uint16_t led_count;
  uint8_t led_rgb[kMaxLeds * 3];
};

// Seed the model from the active carrier. Also resets runtime state.
void begin(const Config& config, const BoardConfig& board);

// Thread-safe copy of the whole model.
Snapshot snapshot();

// Mocked sensor inputs. Defaults: 45.0 / 30.0 / 35.0 °C, all valid, 0 W.
void set_temperature(TempRole role, float celsius, bool valid = true);
void set_power_w(CurrentChannel channel, float watts);
void reset();

// --- Backend hooks, called by hpn::io only --------------------------------
void record_pin(uint8_t pin, bool high);
void record_shift_register(uint8_t value);
void record_led_count(uint16_t count);
void record_led_frame(const uint8_t* rgb, uint16_t count);

// --- Backend reads --------------------------------------------------------
bool read_temperature(TempRole role, float& celsius);
float read_power_w(CurrentChannel channel);

}  // namespace sim
}  // namespace hpn
