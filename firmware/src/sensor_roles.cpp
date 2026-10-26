#include "hpn/sensor_roles.h"

#include <math.h>

namespace hpn {
namespace {

bool valid(const SensorReading& reading) {
  return reading.is_valid && isfinite(reading.value_c);
}

}  // namespace

SensorRoleResult verify_sensor_roles(const Config& config, const SystemState& state) {
  SensorRoleResult result{};
  result.tolerance_c = config.sensor_role_tolerance_c;
  if (!isfinite(result.tolerance_c) || result.tolerance_c < 0.0f) {
    result.tolerance_c = 0.0f;
  }

  result.heating_active = state.heater_phys && state.pump_floor_phys;
  result.all_valid = valid(state.T_boiler) && valid(state.T_mix_out) && valid(state.T_return_floor);
  result.measurable = result.heating_active && result.all_valid;

  if (result.all_valid) {
    result.boiler_c = state.T_boiler.value_c;
    result.mix_c = state.T_mix_out.value_c;
    result.return_c = state.T_return_floor.value_c;
    result.boiler_ge_mix = result.boiler_c + result.tolerance_c >= result.mix_c;
    result.mix_ge_return = result.mix_c + result.tolerance_c >= result.return_c;
    result.boiler_ge_return = result.boiler_c + result.tolerance_c >= result.return_c;
  }

  result.plausible =
      result.measurable && result.boiler_ge_mix && result.mix_ge_return && result.boiler_ge_return;
  return result;
}

const char* sensor_role_summary(const SensorRoleResult& result) {
  if (!result.heating_active) {
    return "not measurable (heating inactive)";
  }
  if (!result.all_valid) {
    return "not measurable (sensor reading invalid)";
  }
  return result.plausible ? "plausible" : "roles suspect";
}

}  // namespace hpn
