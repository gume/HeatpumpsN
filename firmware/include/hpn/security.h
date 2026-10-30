#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

// Access protection on plain HTTP: HPN1 challenge-response request signing.
// Design, threat model and the reasons for the choices: docs/security.md.
//
//   * Two users: "admin" (everything) and "monitor" (read-only). The device
//     stores per user a salt and two keys derived from the password with
//     PBKDF2-HMAC-SHA256; passwords never cross the network.
//   * Every API request carries X-HPN-Auth: user, a single-use nonce issued by
//     the device (valid 60 s) and an HMAC over method, path, canonical query,
//     nonce and the SHA-256 of the body. No clocks, no shared counters.
//   * Secrets in requests and responses are sealed (encrypt-then-MAC built
//     from HMAC-SHA256) with the user's encryption key, bound to the nonce.
//   * MQTT MPC commands are HMAC-signed with a shared secret and an increasing
//     sequence number (MQTT has no request/response to carry a nonce).
namespace hpn {
namespace security {

enum Role : uint8_t { ROLE_NONE = 0, ROLE_MONITOR = 1, ROLE_ADMIN = 2 };

void begin();
void clear();   // factory reset: default passwords, no command secret
// Password recovery build (HPN_PASSWORD_RESET, flashed over USB): clear()
// on the first boot of this image only; true when it reset.
bool reset_passwords_once();

// --- HPN1 request authentication -------------------------------------------
struct Auth {
  Role role;
  uint8_t user;        // index into the user table
  char nonce[33];
  int status;          // 0 ok, else HTTP status for the rejection
  const char* reason;  // "nonce", "user", "sig", "locked", "header"
};

// Fills {"user","salt","iter","nonce","ttl"} for a challenge; false for an unknown user.
bool challenge(const char* user, JsonObject out);
// Issues a fresh nonce (hex) into `out`, e.g. for the X-HPN-Nonce response header.
void issue_nonce(char out[33]);
// Verifies one request. `auth_header` is "user=..,nonce=..,sig=..".
Auth verify(const char* auth_header, const char* method, const char* path, const char* canonical_query,
            const char* body_hash_hex);
const char* user_name(uint8_t user);

// Sealed payloads (base64 iv|ct|tag), bound to the request nonce.
bool unseal(uint8_t user, const char* nonce, const char* b64, char* out, size_t out_size, size_t* out_len);
String seal(uint8_t user, const char* nonce, const char* plaintext, size_t len);

// Admin: new keys for a user, computed by the client from the new password.
bool set_user_keys(const char* user, const char* salt_hex, const char* k_auth_hex, const char* k_enc_hex,
                   char* err, size_t err_size);

// --- Signed MQTT commands (shared secret) ------------------------------------
bool set_command_secret(const char* secret, char* err, size_t err_size);   // "" removes it
bool command_secret_set();
bool open_command(const char* payload, JsonDocument& out, char* err, size_t err_size);
bool open_command(JsonVariantConst in, JsonDocument& out, char* err, size_t err_size);

void sha256_hex(const uint8_t* data, size_t len, char out[65]);
void status_json(JsonObject out);

}  // namespace security
}  // namespace hpn
