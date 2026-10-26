#pragma once

#include "hpn/board.h"
#include "hpn/config.h"
#include "hpn/io_types.h"
#include "hpn/state.h"

// L0: the only layer allowed to touch hardware pins (plan 02 §2.1).
//
// The carrier profile selects direct GPIOs or the 74HC595 output expander; the
// I/O backend selects real pins or the emulation model. Higher layers only call
// the logical helpers below; they never see shift-register bits, MUX select
// lines or pin numbers.
namespace hpn {
namespace l0 {

void begin(const Config& config, const BoardConfig& board);

// Force every assigned actuator to its defined-safe state.
void apply_safe_state();

// Called from T_io. Currently idle; sensor/MUX scheduling lands here.
void tick(const Config& config, const BoardConfig& board, SystemState& state);

// Logical actuator interfaces for L1. Do not call from L2/L4.
void set_relay(Relay relay, bool on);
void set_mixer_motor(bool enabled, bool open_direction);

// Addressable LED chain. rgb is count * 3 bytes in logical RGB order.
void set_leds(const uint8_t* rgb, uint16_t count);

// Sensor access. On hardware these are not implemented yet and report "no
// data"; under emulation they return the values mocked in hpn::sim.
bool read_temperature(TempRole role, float& celsius);

// Sample one current channel using the shared ACS712/CT calibration in
// `config` (pump_current_w_per_v, pump_current_offset_v).
float read_power_w(const Config& config, CurrentChannel channel);

// Discover the sensors physically present on the temperature bus. The hardware
// backend blocks for one conversion cycle; use it only for explicit discovery.
uint8_t discover_temperature_sensors(TemperatureSensorInfo* out, uint8_t max_count);

// Current-measurement MUX. Returns false when the active carrier has no MUX or
// the requested channel is absent.
bool select_current_channel(CurrentChannel channel);
uint16_t read_current_raw(CurrentChannel channel);

}  // namespace l0
}  // namespace hpn
