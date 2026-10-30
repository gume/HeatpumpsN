#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

#include "hpn/config.h"
#include "hpn/mpc_core.h"
#include "hpn/state.h"

// Firmware glue for the predictive mixer controller (mpc_core.h).
//
// Ownership
//   * The controller and its model run in T_control (L2). Plans are computed
//     outside any lock; only short copies cross tasks.
//   * Parameter sets are Category 3 (observer-owned, L3-validated). The observer
//     sends them over MQTT (<base>/cmd/mpc/params) or REST (/api/mpc/params);
//     they are bounds-checked here, applied on the next control tick and kept in
//     their own NVS blob ("hpnmpc"), separate from the Config blob.
//   * One set per radiator scenario (floor only, +rad1, +rad2, +both). The set
//     follows the radiator pumps automatically.
//   * Anomaly limits and the cautious fallback are compiled in (mpc_core.cpp)
//     and cannot be changed by a parameter update.
//
// MQTT
//   <base>/cmd/mpc/params        JSON {"set": "floor|rad1|rad2|both|all", <fields>,
//                                      "reset": bool, "persist": bool}
//   <base>/cmd/mpc/mode          "mpc" | "legacy" | "cautious" (homes first)
//   <base>/state/mpc             JSON status (1 s)
//   <base>/state/mpc/params/<set> retained JSON of each set (on change/connect)
//   <base>/state/mpc/ack         JSON result of the last command
namespace hpn {
namespace mpc {

void begin();

// L2 hooks (T_control). service_homing() runs every tick, before the mixer
// loop; it returns true while a homing stroke owns the valve. control() runs
// on the normal floor-demand path and returns true when it set
// state.mix_target_pos (false: legacy loop is selected).
bool service_homing(const Config& config, SystemState& state);
bool control(const Config& config, SystemState& state, bool flush_active);
void floor_idle();

// Commands (any task). Results are written to `err` (empty on success) and
// queued for <base>/state/mpc/ack. Parameter updates go through
// security::open_command (HMAC signature once a command secret is set).
bool apply_params(JsonVariantConst in, const char* source, char* err, size_t err_size);
bool apply_params_json(const char* json, const char* source, char* err, size_t err_size);
bool apply_mode(const char* mode, const char* source, char* err, size_t err_size);
// MQTT mode command: signed envelope once a command secret is set.
bool apply_mode_command(const char* payload, const char* source, char* err, size_t err_size);

// Telemetry (any task).
void status_json(JsonObject out, bool with_trajectory);
void params_json(uint8_t scenario, JsonObject out);
uint32_t params_revision();           // bumps on every accepted change
bool take_ack(char* out, size_t out_size);
uint8_t mode();

}  // namespace mpc
}  // namespace hpn
