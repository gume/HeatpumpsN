#include "hpn/board.h"

#include <stdio.h>

#include "hpn/state.h"

namespace hpn {
namespace {

// --- HomeNet Heatpumps 1.0 -------------------------------------------------
// Legacy carrier: any MCU in the S2 Mini footprint, currently a LOLIN S2 Mini.
// See firmware/docs/hhn-1.0.md for the full pin and bit map.
const BoardConfig kHhn1 = {
    "HomeNet Heatpumps 1.0",
    /* has current   */ true,
    /* adc pin       */ 3,
    /* mux s2        */ 5,
    /* mux s3        */ 5,
    /* mux channels  */ {1, 3, 0, 2},  // floor, rad1, rad2, mixer
};

// --- Boardless development -------------------------------------------------
// Bare ESP32 devkit without a carrier. Pins stay unassigned until the new
// HomeNet Heatpumps 2.0 layout is defined. Use HPN_IO_EMULATION to develop
// against an emulated carrier instead.
const BoardConfig kBoardless = {
    "boardless devkit",
    false,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    {0xFF, 0xFF, 0xFF, 0xFF},
};

template <size_t N>
void copy_str(char (&dst)[N], const char* src) {
  snprintf(dst, N, "%s", src);
}

}  // namespace

const BoardConfig& board_config() {
#if defined(HPN_CARRIER_HHN1)
  return kHhn1;
#elif defined(HPN_CARRIER_BOARDLESS)
  return kBoardless;
#else
#error "No HPN_CARRIER_* macro defined. Set a carrier profile in platformio.ini."
#endif
}

void board_apply_defaults(Config& config) {
#if defined(HPN_CARRIER_HHN1)
  // Legacy carrier wiring. The output extender is runtime-selectable, so the
  // 74HC595 values below are defaults rather than the only possible setup.
  config.output_expander = OUTPUT_EXPANDER_74HC595;
  config.expander_address = 0x20;  // unused by the shift register
  config.expander_data_pin = 12;
  config.expander_clock_pin = 11;
  config.expander_latch_pin = 9;
  config.expander_oe_pin = 7;
  config.expander_oe_active_low = true;
  config.output_active_low = false;  // relay board is active-high on HHN 1.0
  config.expander_bit_heater = 0;
  config.expander_bit_pump_rad1 = 5;
  config.expander_bit_pump_rad2 = 6;
  config.expander_bit_pump_floor = 7;
  config.expander_bit_mix_direction = 3;
  config.expander_bit_mix_motion = 4;
  config.expander_bit_mux_s0 = 2;
  config.expander_bit_mux_s1 = 1;

  config.gpio_onewire = 18;   // D4 / 1-Wire bus
  config.gpio_led_data = 16;  // 5 x WS2812B/SK6812, GRB
  config.num_leds = 5;
  config.has_pump_power[0] = true;  // floor
  config.has_pump_power[1] = true;  // radiator 1
  config.has_pump_power[2] = true;  // radiator 2

  // Legacy ESPHome configuration used a 145 s full mixer stroke.
  config.travel_time_ms = 145000;
  config.setpoint_default = 35.0f;
  config.floor_setpoint_shadow = 35.0f;
  config.mixer_wiring = MIX_WIRING_DIRECTION_MOTION;

  // ROM IDs from analysis/OLD-CODE/heatpumps.yaml. Config stores the 16 hex
  // digits without the 0x prefix so they fit in char[17].
  copy_str(config.sensor_id_boiler, "c13cdaf648ed0528");  // "incoming" (boiler side)
  copy_str(config.sensor_id_ret, "92c08b7f1f64ff28");     // floor return
  copy_str(config.sensor_id_mix, "593c26f648ddbf28");     // mixed flow
#endif
  // The boardless profile leaves pins unassigned until Heatpumps 2.0 exists.
}

}  // namespace hpn
