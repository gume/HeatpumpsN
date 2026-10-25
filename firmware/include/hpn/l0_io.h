#pragma once

#include "hpn/board.h"
#include "hpn/config.h"
#include "hpn/state.h"

// L0: the only layer allowed to touch hardware pins (plan 02 §2.1).
//
// The board profile selects direct GPIOs or the old board's 74HC595 output
// expander. Higher layers only call the logical helpers below; they never see
// shift-register bits or MUX select lines.
namespace hpn {
namespace l0 {

enum class Relay : uint8_t {
  Heater = 0,
  PumpRad1,
  PumpRad2,
  PumpFloor,
};

void begin(const Config& config, const BoardConfig& board);

// Force every assigned actuator to its defined-safe state.
void apply_safe_state();

// Called from T_io. Currently idle; sensor/MUX scheduling lands here.
void tick(const Config& config, const BoardConfig& board, SystemState& state);

// Logical actuator interfaces for L1. Do not call from L2/L4.
void set_relay(Relay relay, bool on);
void set_mixer_motor(bool enabled, bool open_direction);

// Current-measurement MUX. Returns false when the active board has no MUX or
// the requested channel is absent.
bool select_current_channel(CurrentChannel channel);
uint16_t read_current_raw(CurrentChannel channel);

}  // namespace l0
}  // namespace hpn
