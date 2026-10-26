#include "hpn/state.h"

#include <math.h>

void state_init(SystemState& state) {
  state = SystemState{};

  state.mode = MODE_HEAT;
  state.dhw_active = false;
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
