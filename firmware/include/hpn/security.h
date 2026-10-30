#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

// Basic access protection (no TLS: it stops casual access and unsigned code,
// not an eavesdropper on the network).
//
//   * Web login: HTTP Digest, user "admin", default password "admin". The
//     device stores only the Digest hash (HA1). /ws uses a session token that
//     the logged-in portal fetches from /api/session.
//   * Signed updates: application and LittleFS images carry an ECDSA P-256
//     signature over their SHA-256 (POST /api/update?sha256=<hex>&sig=<base64>).
//     Enforced once an OTA public key is installed; the portal can allow
//     unsigned uploads for a limited time.
//   * Signed observer commands: MPC parameter and mode commands are an
//     envelope {"msg": "<json>", "sig": "<base64>"}, signed with the observer's
//     own key; msg carries a strictly increasing "seq" (replay protection).
//     Enforced once an observer public key is installed. Home Assistant
//     commands are not affected.
//
// State lives in its own NVS namespace ("hpnsec"); a factory reset clears it.
namespace hpn {
namespace security {

enum KeySlot : uint8_t { KEY_OTA = 0, KEY_OBSERVER = 1, KEY_COUNT };

void begin();
void clear();   // factory reset: default password, no keys

// --- Web login -------------------------------------------------------------
const char* realm();
const char* username();
String digest_ha1();
bool set_password(const char* current, const char* next, char* err, size_t err_size);
const char* ws_token();
bool check_ws_token(const char* token);

// --- Keys --------------------------------------------------------------------
bool set_key(KeySlot slot, const char* pem, char* err, size_t err_size);   // "" removes it
bool key_installed(KeySlot slot);
bool verify(KeySlot slot, const uint8_t hash[32], const uint8_t* sig, size_t sig_len);
// Decodes base64; returns the decoded length or 0.
size_t base64_decode(const char* in, uint8_t* out, size_t out_size);
bool parse_hex32(const char* hex, uint8_t out[32]);

// --- Updates -------------------------------------------------------------------
bool ota_signature_required();
void allow_unsigned(uint32_t minutes);   // 0 re-arms enforcement
uint32_t unsigned_remaining_s();

// --- Observer commands ------------------------------------------------------------
// Opens a command payload (signed envelope, or plain JSON while no observer
// key is installed) into `out`. Rejects bad signatures and replays.
bool open_command(const char* payload, JsonDocument& out, char* err, size_t err_size);
bool open_command(JsonVariantConst in, JsonDocument& out, char* err, size_t err_size);

void status_json(JsonObject out);

}  // namespace security
}  // namespace hpn
