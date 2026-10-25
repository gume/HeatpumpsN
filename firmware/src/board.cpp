#include "hpn/board.h"

#include <stdio.h>

#include "hpn/state.h"

namespace hpn {
namespace {

// --- Original carrier: WEMOS/LOLIN S2 Mini + 74HC595 + 74HC4051 ------------
//
// See firmware/docs/board-old-lolin-s2.md for the full pin and bit map.
const BoardConfig kOldLolinS2 = {
    "old-lolin-s2",
    OutputBackend::ShiftRegister,
    /* sr_data_pin   */ 12,
    /* sr_clock_pin  */ 11,
    /* sr_latch_pin  */ 9,
    /* sr_oe_pin     */ 7,
    /* oe active low */ true,
    /* heater        */ 0,
    /* pump_rad1     */ 5,
    /* pump_rad2     */ 6,
    /* pump_floor    */ 7,
    /* mix direction */ 3,
    /* mix motion    */ 4,
    /* mux s0        */ 2,
    /* mux s1        */ 1,
    /* has current   */ true,
    /* adc pin       */ 3,
    /* mux s2        */ 5,
    /* mux s3        */ 5,
    /* mux channels  */ {1, 3, 0, 2},  // floor, rad1, rad2, mixer
};

// --- Generic development board (pins still to be assigned) -----------------
const BoardConfig kGenericDevkit = {
    "generic-devkit",
    OutputBackend::DirectGpio,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    true,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
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
#if defined(HPN_BOARD_OLD_LOLIN_S2)
  return kOldLolinS2;
#elif defined(HPN_BOARD_GENERIC_DEVKIT)
  return kGenericDevkit;
#else
#error "No HPN_BOARD_* macro defined. Set a board profile in platformio.ini."
#endif
}

void board_apply_defaults(Config& config) {
#if defined(HPN_BOARD_OLD_LOLIN_S2)
  config.gpio_onewire = 18;  // D4 / 1-Wire bus
  config.gpio_led_data = 16; // 5 x WS2812B/SK6812, GRB
  config.num_leds = 5;
  config.has_pump_power[0] = true;  // floor
  config.has_pump_power[1] = true;  // radiator 1
  config.has_pump_power[2] = true;  // radiator 2

  // Old ESPHome configuration used a 145 s full mixer stroke.
  config.travel_time_ms = 145000;
  config.setpoint_default = 35.0f;
  config.floor_setpoint_shadow = 35.0f;

  // ROM IDs from analysis/OLD-CODE/heatpumps.yaml. Config stores the 16 hex
  // digits without the 0x prefix so they fit in char[17].
  copy_str(config.sensor_id_boiler, "c13cdaf648ed0528");  // "incoming" (boiler side)
  copy_str(config.sensor_id_ret, "92c08b7f1f64ff28");     // floor return
  copy_str(config.sensor_id_mix, "593c26f648ddbf28");     // mixed flow
#endif
  // The generic profile leaves pins unassigned until the new board is defined.
}

}  // namespace hpn
