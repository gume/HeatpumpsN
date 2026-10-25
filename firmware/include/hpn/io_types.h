#pragma once

#include <stdint.h>

// Logical I/O types shared by L0, the board profiles and the emulation model.
//
// These are *roles*, not pins: the active board profile maps them to shift
// register bits, MUX channels or GPIOs.
namespace hpn {

// Local temperature sensors on the 1-Wire bus (plan 01 §3).
enum class TempRole : uint8_t {
  Boiler = 0,
  ReturnFloor,
  MixOut,
  Count,
};

// Analogue current-sense channels (ACS712 through the carrier MUX).
enum class CurrentChannel : uint8_t {
  PumpFloor = 0,
  PumpRad1,
  PumpRad2,
  Mixer,
  Count,
};

// Logical relay outputs.
enum class Relay : uint8_t {
  Heater = 0,
  PumpRad1,
  PumpRad2,
  PumpFloor,
  Count,
};

constexpr uint8_t kTempRoleCount = static_cast<uint8_t>(TempRole::Count);
constexpr uint8_t kCurrentChannelCount = static_cast<uint8_t>(CurrentChannel::Count);
constexpr uint8_t kRelayCount = static_cast<uint8_t>(Relay::Count);

// One entry returned by a 1-Wire discovery scan. The ID is the 16 hex digits
// of the 64-bit ROM address, without the 0x prefix.
struct TemperatureSensorInfo {
  char id[17];
  float celsius;
  bool valid;
};

// Upper bound for emulated addressable LEDs and recorded GPIOs in the model.
constexpr uint8_t kMaxLeds = 8;
constexpr uint8_t kMaxGpio = 48;

}  // namespace hpn
