#include "hpn/state.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

void state_init(SystemState& state) {
  state = SystemState{};

  state.mode = MODE_HEAT;
  state.dhw_active = false;
  state.dhw_meter_active = false;
  state.dhw_inferred = false;
  state.test_mode_rem_s = 0;

  state.T_boiler.value_c = NAN;
  state.T_boiler.is_valid = false;
  state.T_return_floor.value_c = NAN;
  state.T_return_floor.is_valid = false;
  state.T_mix_out.value_c = NAN;
  state.T_mix_out.is_valid = false;
  state.t_ret_flush_valid = true;

  state.pos_est_dr = 0.0f;
  state.pos_encoder = 0.0f;
  state.pos_encoder_valid = false;
  state.mix_target_pos = 0.0f;
  state.mix_ceiling = 1.0f;
  state.mix_overshoot = false;
  state.mix_motion = false;
  state.mix_dir = 0;
  state.mix_emergency = false;
  state.mix_free_run_dir = 0;
  state.mix_free_run_until_ms = 0;

  state.divergence_reason = DIV_NONE;
  state.system_error_code = ERR_OK;
  state.active_alarms = 0;
  state.flush_state = FLUSH_IDLE;
}

const char* error_code_name(uint16_t code) {
  switch (code) {
    case ERR_OK:
      return "no error";
    case ERR_L1_HARD_OVERTEMP:
      return "L1 hard floor over-temperature";
    case ERR_SENSOR_MIX_LOST:
      return "mixer forward sensor lost";
    case ERR_SENSOR_RET_LOST:
      return "floor return sensor lost";
    case ERR_SENSOR_BOILER_LOST:
      return "boiler supply sensor lost";
    case ERR_HA_KEEPALIVE_TIMEOUT:
      return "heating switch keep-alive timeout";
    case ERR_RELAY_FAULT_WELDED:
      return "welded relay contact";
    case ERR_NVS_DATA_CORRUPTED:
      return "NVS checksum mismatch";
    case ERR_SENSOR_ROLE_SUSPECT:
      return "sensor role plausibility";
    case ERR_DHW_INFER_TIMEOUT:
      return "inferred DHW charging timeout";
    default:
      return "unknown error";
  }
}

const char* alarm_name(uint32_t bit) {
  switch (bit) {
    case ALARM_OVERTEMP_HARD:
      return "hard over-temperature";
    case WARN_OVERTEMP_HIGH:
      return "high over-temperature";
    case WARN_GRADIENT_CUTOFF:
      return "gradient cutoff";
    case ALARM_SENSOR_FAULT_MIX:
      return "mixer sensor fault";
    case WARN_SENSOR_FAULT_RET:
      return "return sensor fault";
    case WARN_SENSOR_FAULT_BOILER:
      return "boiler sensor fault";
    case WARN_HEATER_NO_PUMP:
      return "heater without pump";
    case WARN_ENCODER_STALE:
      return "encoder stale";
    case WARN_ENCODER_DIVERGED:
      return "encoder diverged";
    case WARN_PUMP_DRY_RUN:
      return "pump dry run";
    case ALARM_WELDED_RELAY:
      return "welded relay";
    case WARN_NVS_CORRUPTED:
      return "NVS corrupted";
    case WARN_MIX_MOTION_OVERRUN:
      return "mixer motion overrun";
    case WARN_MIX_NO_RESPONSE:
      return "mixer no encoder response";
    case WARN_MIX_OVERSHOOT:
      return "mixer forward overshoot";
    case WARN_SENSOR_ROLE_SUSPECT:
      return "sensor role suspect";
    case WARN_DHW_INFER_TIMEOUT:
      return "inferred DHW too long";
    default:
      return "unknown";
  }
}

namespace {

void append(char* out, size_t out_size, const char* text) {
  if (out == nullptr || out_size == 0 || text == nullptr) {
    return;
  }
  const size_t used = strlen(out);
  if (used + 1 >= out_size) {
    return;
  }
  snprintf(out + used, out_size - used, "%s", text);
}

void append_alarm_list(char* out, size_t out_size, uint32_t alarms) {
  bool first = true;
  for (uint8_t i = 0; i < 32; ++i) {
    const uint32_t bit = 1u << i;
    if ((alarms & bit) == 0) {
      continue;
    }
    const char* name = alarm_name(bit);
    if (strcmp(name, "unknown") == 0) {
      continue;
    }
    append(out, out_size, first ? " " : ", ");
    append(out, out_size, name);
    first = false;
  }
}

bool has_critical_alarm(uint32_t alarms) {
  return (alarms & (ALARM_OVERTEMP_HARD | ALARM_SENSOR_FAULT_MIX | ALARM_WELDED_RELAY)) != 0;
}

}  // namespace

void format_error_text(const SystemState& state, char* out, size_t out_size) {
  if (out == nullptr || out_size == 0) {
    return;
  }

  const uint16_t code = state.system_error_code;
  const uint32_t alarms = state.active_alarms;
  if (code == ERR_OK && alarms == 0) {
    snprintf(out, out_size, "No error");
    return;
  }

  if (code == ERR_OK) {
    snprintf(out, out_size, "%s:", has_critical_alarm(alarms) ? "Alarm" : "Warning");
    append_alarm_list(out, out_size, alarms);
    return;
  }

  snprintf(out, out_size, "%u: %s", static_cast<unsigned>(code), error_code_name(code));

  switch (code) {
    case ERR_L1_HARD_OVERTEMP:
      if (isfinite(state.T_mix_out.value_c)) {
        char detail[48];
        snprintf(detail, sizeof(detail), " (T_mix_out %.1f C)",
                 static_cast<double>(state.T_mix_out.value_c));
        append(out, out_size, detail);
      }
      break;
    case ERR_SENSOR_MIX_LOST:
      append(out, out_size, " (mixer forward reading invalid)");
      break;
    case ERR_SENSOR_RET_LOST:
      append(out, out_size, " (floor return reading invalid)");
      break;
    case ERR_SENSOR_BOILER_LOST:
      append(out, out_size, " (boiler supply reading invalid)");
      break;
    case ERR_HA_KEEPALIVE_TIMEOUT:
      append(out, out_size, " (heating switch not refreshed by HA)");
      break;
    case ERR_RELAY_FAULT_WELDED:
      append(out, out_size, " (pump current with relay commanded off)");
      break;
    case ERR_NVS_DATA_CORRUPTED:
      append(out, out_size, " (saved NVS data rejected; defaults loaded)");
      break;
    case ERR_SENSOR_ROLE_SUSPECT:
      append(out, out_size, " (sensor order implausible while heating)");
      break;
    case ERR_DHW_INFER_TIMEOUT:
      append(out, out_size, " (inferred DHW charging exceeded the configured maximum)");
      break;
    default:
      break;
  }

  if (alarms != 0) {
    append(out, out_size, " [flags:");
    append_alarm_list(out, out_size, alarms);
    append(out, out_size, "]");
  }
}
