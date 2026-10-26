#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

// Sensor-role plausibility check.
//
// The three DS18B20 roles can only be validated while heat is actually flowing:
// with the heater and floor pump running, the boiler-side sensor must be the
// warmest, the mixed flow next, and the floor return coldest (within the
// configured tolerance). Outside that condition the check reports
// "not measurable" rather than guessing.
namespace hpn {

struct SensorRoleResult {
  bool heating_active;  // heater and floor pump physically on
  bool all_valid;       // all three readings valid and finite
  bool measurable;      // heating_active && all_valid
  bool plausible;       // order is correct within the tolerance
  float tolerance_c;
  float boiler_c;
  float mix_c;
  float return_c;
  bool boiler_ge_mix;
  bool mix_ge_return;
  bool boiler_ge_return;
};

SensorRoleResult verify_sensor_roles(const Config& config, const SystemState& state);

// Human-readable summary, e.g. "not measurable (heating inactive)".
const char* sensor_role_summary(const SensorRoleResult& result);

}  // namespace hpn
