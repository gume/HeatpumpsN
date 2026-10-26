#include "hpn/led_status.h"

#include <Arduino.h>
#include <string.h>

#include "hpn/l0_io.h"
#include "hpn/mqtt_client.h"
#include "hpn/wifi_client.h"

namespace hpn {
namespace led {
namespace {

struct Color {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

constexpr Color kOff{0, 0, 0};
constexpr Color kGreen{0, 160, 0};
constexpr Color kOrange{200, 70, 0};
constexpr Color kYellow{200, 160, 0};
constexpr Color kRed{220, 0, 0};
constexpr Color kCyan{0, 150, 150};
constexpr Color kMagenta{160, 0, 160};
constexpr Color kPurple{110, 0, 190};

uint8_t g_frame[kMaxLeds * 3] = {};
bool g_have_frame = false;
uint32_t g_last_tick_ms = 0;

uint8_t triangle(uint32_t now, uint32_t period, uint8_t low, uint8_t high) {
  if (period < 2) {
    return high;
  }
  const uint32_t half = period / 2;
  const uint32_t phase = now % period;
  const uint32_t ramp = phase < half ? phase : period - phase;
  return static_cast<uint8_t>(low + (high - low) * ramp / half);
}

void put(uint8_t* frame, uint8_t index, Color color, uint8_t brightness) {
  if (index >= kMaxLeds) {
    return;
  }
  frame[index * 3 + 0] = static_cast<uint8_t>(color.r * brightness / 255);
  frame[index * 3 + 1] = static_cast<uint8_t>(color.g * brightness / 255);
  frame[index * 3 + 2] = static_cast<uint8_t>(color.b * brightness / 255);
}

bool blink(uint32_t now, uint32_t period) {
  return period < 2 || (now % period) < period / 2;
}

}  // namespace

void tick(const Config& config, SystemState& state) {
  const uint32_t now = millis();
  if (now - g_last_tick_ms < 50 && g_have_frame) {
    return;
  }
  g_last_tick_ms = now;

  uint8_t frame[kMaxLeds * 3] = {};

  const bool critical = (state.active_alarms &
                         (ALARM_OVERTEMP_HARD | ALARM_SENSOR_FAULT_MIX |
                          ALARM_WELDED_RELAY)) != 0;
  const mqtt::Status& mqtt_status = mqtt::status();
  const bool wifi_up = wifi::status().connected;
  const bool mqtt_down = mqtt_status.enabled && !mqtt_status.connected;

  // LED 0: system and communications.
  if (critical) {
    if (blink(now, 500)) {
      put(frame, 0, kRed, 255);
    }
  } else if (!wifi_up || mqtt_down) {
    put(frame, 0, kYellow, triangle(now, 1600, 24, 230));
  } else {
    put(frame, 0, kGreen, 255);
  }

  // LED 1: shared boiler / heater.
  if (state.heater_phys) {
    put(frame, 1, kOrange, 255);
  } else if (state.dhw_active) {
    put(frame, 1, kCyan, 255);
  } else if (state.divergence_reason == DIV_ANTI_SHORT_CYCLE) {
    if (blink(now, 800)) {
      put(frame, 1, kPurple, 255);
    }
  }

  // LED 2: floor circuit and mixer.
  if ((state.active_alarms & ALARM_OVERTEMP_HARD) != 0) {
    if (blink(now, 200)) {
      put(frame, 2, kRed, 255);
    }
  } else if (state.flush_state == FLUSH_ACTIVE) {
    put(frame, 2, kCyan, triangle(now, 1200, 32, 220));
  } else if (state.divergence_reason == DIV_FLOOR_DELTA_T_COLLAPSED) {
    put(frame, 2, kMagenta, 255);
  } else if (state.pump_floor_phys) {
    put(frame, 2, kGreen, 255);
  }

  // LEDs 3 and 4: radiator circuits.
  const bool pump_fault = (state.active_alarms & (ALARM_WELDED_RELAY | WARN_PUMP_DRY_RUN)) != 0;
  const bool rad_on[2] = {state.pump_rad1_phys, state.pump_rad2_phys};
  for (uint8_t i = 0; i < 2; ++i) {
    const uint8_t index = static_cast<uint8_t>(3 + i);
    if (pump_fault) {
      put(frame, index, kRed, 255);
    } else if (rad_on[i]) {
      put(frame, index, kOrange, 255);
    }
  }

  uint16_t count = config.num_leds;
  if (count > kMaxLeds) {
    count = kMaxLeds;
  }
  if (count == 0) {
    return;
  }
  if (g_have_frame && memcmp(frame, g_frame, sizeof(frame)) == 0) {
    return;
  }

  memcpy(g_frame, frame, sizeof(g_frame));
  g_have_frame = true;
  l0::set_leds(g_frame, count);
}

}  // namespace led
}  // namespace hpn
