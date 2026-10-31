#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

// L4 status-LED animator.
//
// The carrier profile supplies the data GPIO and LED count; L0 owns the actual
// NeoPixel write. This module maps the controller state to the five-LED
// semantics in plans/controller node/02_hardware_and_l0.md §1.2:
//
//   0 system/comms, 1 boiler/heater, 2 floor circuit, 3 radiator 1, 4 radiator 2
namespace hpn {
namespace led {

// Called from T_comms after the MQTT/Wi-Fi status has been refreshed, with a
// published state snapshot rather than the live struct.
void tick(const Config& config, const SystemState& state);

// LED test (Test Mode only, see web_portal): while on, all LEDs step together
// through every colour, 1 s each, in place of the status display.
void set_test(bool on);
bool test_active();

}  // namespace led
}  // namespace hpn
