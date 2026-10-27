#include "hpn/board.h"

#include <stdio.h>

#include "hpn/state.h"

namespace hpn {
namespace {

// --- HomeNet Heating control v0.1 -----------------------------------------
// Legacy board: any MCU in the S2 Mini footprint, currently a LOLIN S2 Mini.
// See firmware/docs/boards.md for the full pin and bit map.
const BoardConfig kHhn1 = {
    "HomeNet Heating control v0.1",
    /* has current   */ true,
    /* adc pin       */ 3,
    /* mux s2        */ 5,
    /* mux s3        */ 5,
    /* mux channels  */ {1, 3, 0, 2},  // floor, rad1, rad2, mixer
    /* ads channels  */ {0xFF, 0xFF, 0xFF, 0xFF},
};

// --- HomeNet Heating control v1.x -----------------------------------------
// Current board. The relay board is reached through a PCF8574; current is
// measured by an ADS1115 (A0/A1/A3 do exist; A4 is not an ADS1115 input) whose
// TLV9064 sensor assembly is an
// optional external part. Pins are defaults; confirm them against the board
// schematic before flashing a real unit.
const BoardConfig kHhn1x = {
    "HomeNet Heating control v1.x",
    /* has current   */ false,
    /* adc pin       */ HPN_GPIO_UNASSIGNED,
    /* mux s2        */ HPN_GPIO_UNASSIGNED,
    /* mux s3        */ HPN_GPIO_UNASSIGNED,
    /* mux channels  */ {0xFF, 0xFF, 0xFF, 0xFF},
    /* ads channels  */ {3, 0, 1, 4},  // floor=A3, rad1=A0, rad2=A1, mixer=A4
};

// --- Boardless development -------------------------------------------------
// Bare ESP32 devkit without a board. Pins stay unassigned until a board layout
// is selected. Use HPN_IO_EMULATION to develop against an emulated board
// instead.
const BoardConfig kBoardless = {
    "boardless devkit",
    false,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    HPN_GPIO_UNASSIGNED,
    {0xFF, 0xFF, 0xFF, 0xFF},
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
#elif defined(HPN_CARRIER_HHN1X)
  return kHhn1x;
#elif defined(HPN_CARRIER_BOARDLESS)
  return kBoardless;
#else
#error "No HPN_CARRIER_* macro defined. Set a board profile in platformio.ini."
#endif
}

void board_apply_defaults(Config& config) {
#if defined(HPN_CARRIER_HHN1)
  // HomeNet Heating control v0.1 wiring. The output driver is runtime
  // selectable, so the 74HC595 values below are defaults rather than the only
  // possible setup.
  config.output_expander = OUTPUT_EXPANDER_74HC595;
  config.expander_address = 0x20;  // unused by the shift register
  config.expander_data_pin = 12;
  config.expander_clock_pin = 11;
  config.expander_latch_pin = 9;
  config.expander_oe_pin = 7;
  config.expander_oe_active_low = true;
  config.output_active_low = false;
  config.output_active_low_heater = false;
  config.output_active_low_pump_rad1 = false;
  config.output_active_low_pump_rad2 = false;
  config.output_active_low_pump_floor = false;
  config.output_active_low_mix_direction = false;
  config.output_active_low_mix_motion = false;
  config.expander_bit_heater = 0;
  config.expander_bit_pump_rad1 = 5;
  config.expander_bit_pump_rad2 = 6;
  config.expander_bit_pump_floor = 7;
  config.expander_bit_mix_direction = 3;
  config.expander_bit_mix_motion = 4;
  config.expander_bit_mux_s0 = 2;
  config.expander_bit_mux_s1 = 1;
  config.expander_bit_mix_fault = HPN_GPIO_UNASSIGNED;
  config.mix_fault_active_low = true;

  config.current_sense = CURRENT_SENSE_MUX_ADC;
  config.ads1115_address = 0x48;
  config.tlv9064_present = false;

  config.gpio_onewire = 18;   // D4 / 1-Wire bus
  config.gpio_led_data = 16;  // 5 x WS2812B/SK6812, GRB
  config.num_leds = 5;
  config.has_pump_power[0] = true;  // floor
  config.has_pump_power[1] = true;  // radiator 1
  config.has_pump_power[2] = true;  // radiator 2

  // The old ESPHome configuration used a 145 s full mixer stroke.
  config.travel_time_ms = 145000;
  config.setpoint_default = 35.0f;
  config.floor_setpoint_shadow = 35.0f;
  config.mixer_wiring = MIX_WIRING_DIRECTION_MOTION;

  // ROM IDs from analysis/OLD-CODE/heatpumps.yaml. Config stores the 16 hex
  // digits without the 0x prefix so they fit in char[17].
  copy_str(config.sensor_id_boiler, "c13cdaf648ed0528");  // "incoming" (boiler side)
  copy_str(config.sensor_id_ret, "92c08b7f1f64ff28");     // floor return
  copy_str(config.sensor_id_mix, "593c26f648ddbf28");     // mixed flow
#elif defined(HPN_CARRIER_HHN1X)
  // HomeNet Heating control v1.x. The PCF8574 relay board is active-low on
  // every output; P7 is the interlock fault input. The TLV9064 is optional and
  // defaults to absent, so current protection stays off until it is enabled.
  config.output_expander = OUTPUT_EXPANDER_PCF8574;
  config.expander_address = 0x20;
  config.expander_data_pin = HPN_GPIO_UNASSIGNED;
  config.expander_clock_pin = HPN_GPIO_UNASSIGNED;
  config.expander_latch_pin = HPN_GPIO_UNASSIGNED;
  config.expander_oe_pin = HPN_GPIO_UNASSIGNED;
  config.expander_oe_active_low = true;
  config.output_active_low = true;
  config.output_active_low_heater = true;
  config.output_active_low_pump_rad1 = true;
  config.output_active_low_pump_rad2 = true;
  config.output_active_low_pump_floor = true;
  config.output_active_low_mix_direction = true;
  config.output_active_low_mix_motion = true;
  config.expander_bit_pump_rad1 = 0;  // P0: pump 1
  config.expander_bit_pump_rad2 = 1;  // P1: pump 2
  config.expander_bit_pump_floor = 2; // P2: pump 3 / floor
  config.expander_bit_mix_direction = 3;  // P3: open
  config.expander_bit_mix_motion = 4;     // P4: close
  config.expander_bit_heater = 5;         // P5: heat
  config.expander_bit_mux_s0 = HPN_GPIO_UNASSIGNED;  // no analogue MUX
  config.expander_bit_mux_s1 = HPN_GPIO_UNASSIGNED;
  config.expander_bit_mix_fault = 7;      // P7: interlock fault input
  config.mix_fault_active_low = true;

  config.current_sense = CURRENT_SENSE_ADS1115;
  config.ads1115_address = 0x48;
  config.tlv9064_present = false;
  config.i2c_sda = 8;
  config.i2c_scl = 9;

  config.gpio_onewire = HPN_GPIO_UNASSIGNED;
  config.gpio_led_data = HPN_GPIO_UNASSIGNED;
  config.num_leds = 0;
  config.has_pump_power[0] = true;  // floor
  config.has_pump_power[1] = true;  // radiator 1
  config.has_pump_power[2] = true;  // radiator 2

  config.travel_time_ms = 145000;
  config.setpoint_default = 35.0f;
  config.floor_setpoint_shadow = 35.0f;
  config.mixer_wiring = MIX_WIRING_OPEN_CLOSE;
#endif
  // The boardless profile leaves pins unassigned until a board is selected.
}

}  // namespace hpn
