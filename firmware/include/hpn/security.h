#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

// Basic access protection (no TLS: it stops casual access and stray uploads,
// not an eavesdropper on the network).
//
//   * Web login: HTTP Digest, user "admin", default password "admin". The
//     device stores only the Digest hash (HA1). /ws uses a session token that
//     the logged-in portal fetches from /api/session.
//   * Upload window: firmware and LittleFS uploads (/api/update) are refused
//     unless a logged-in user opened the window for a few minutes. The window
//     lives in RAM, so a reboot closes it.
//   * Signed commands: MPC parameter and mode commands from the observer are an
//     envelope {"msg": "<json>", "sig": "<hex HMAC-SHA256(secret, msg)>"} with a
//     strictly increasing "seq" in msg. Enforced once a shared secret is set.
//     Home Assistant commands are not affected.
//
// State lives in its own NVS namespace ("hpnsec"); a factory reset clears it.
namespace hpn {
namespace security {

void begin();
void clear();   // factory reset: default password, no secret

// --- Web login -------------------------------------------------------------
const char* realm();
const char* username();
String digest_ha1();
bool set_password(const char* current, const char* next, char* err, size_t err_size);
const char* ws_token();
bool check_ws_token(const char* token);

// --- Upload window ---------------------------------------------------------
bool uploads_open();
void open_uploads(uint32_t minutes);   // 0 closes
uint32_t uploads_remaining_s();

// --- Signed commands -------------------------------------------------------
bool set_command_secret(const char* secret, char* err, size_t err_size);   // "" removes it
bool command_secret_set();
// Opens a command payload (signed envelope, or plain JSON while no secret is
// set) into `out`. Rejects bad signatures and replays.
bool open_command(const char* payload, JsonDocument& out, char* err, size_t err_size);
bool open_command(JsonVariantConst in, JsonDocument& out, char* err, size_t err_size);

void status_json(JsonObject out);

}  // namespace security
}  // namespace hpn
