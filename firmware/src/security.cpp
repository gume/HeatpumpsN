#include "hpn/security.h"

#include <Arduino.h>
#include <Preferences.h>
#include <esp_app_desc.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/base64.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <string.h>

#include <memory>
#include <new>

#include "hpn/crc32.h"
#include "hpn/web_portal.h"

namespace hpn {
namespace security {
namespace {

constexpr const char* kNamespace = "hpnsec";
constexpr const char* kKey = "blob";
constexpr const char* kSeqKey = "seq";
constexpr uint32_t kMagic = 0x48504E53u;  // "HPNS"
constexpr uint16_t kVersion = 3;           // no migration: another version loads the defaults
constexpr uint32_t kIterations = 10000;    // PBKDF2, run by the clients only
constexpr uint8_t kUsers = 2;
constexpr size_t kNonces = 32;
constexpr uint32_t kNonceTtlMs = 60000;
constexpr uint8_t kMaxFailures = 5;
constexpr uint32_t kLockMs = 30000;
constexpr size_t kSecretMin = 16;
constexpr size_t kSecretMax = 64;

const char* const kUserNames[kUsers] = {"admin", "monitor"};
const Role kUserRoles[kUsers] = {ROLE_ADMIN, ROLE_MONITOR};

// Factory credentials admin/admin and monitor/monitor, precomputed with
// tools/hpn_auth.derive_keys (salt = SHA-256("hpn-default-salt-<user>")[:16]),
// so the device never has to run PBKDF2.
struct DefaultUser {
  const char* salt;
  const char* k_auth;
  const char* k_enc;
};
const DefaultUser kDefaults[kUsers] = {
    {"c323a66e86b8d3ae7330b21bb173d78e", "9254689812b22f73907625c509ac736a4c9e821fa1774c0bea21a7936c0028ad",
     "554bb0f505779f2a8fb654df360b96808c64714451f467ae1a77fe98ed011e70"},
    {"8ffabaf5db7cf90e080c7adc18e52707", "00303a8b77d78ab080f3cce9243ab4da46cca0eb9ea0c6c3ec4e7d3cbdada577",
     "581631677ea461cde8997239b8d873440b2b7a9a5540808cbf2c2c7bc09b4fdf"},
};

struct UserKeys {
  uint8_t salt[16];
  uint8_t k_auth[32];
  uint8_t k_enc[32];
};

struct Blob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  UserKeys users[kUsers];
  char secret[kSecretMax + 1];
};

struct Nonce {
  char hex[33];
  uint32_t issued_ms;
  bool live;
};

SemaphoreHandle_t g_mutex = nullptr;
// On the heap: the ESP32-S2 has little static DRAM to spare.
Blob* g_blob = nullptr;
Nonce* g_nonces = nullptr;
uint8_t g_failures[kUsers] = {};
uint32_t g_locked_until[kUsers] = {};
uint64_t g_last_seq = 0;

struct Lock {
  Lock() { if (g_mutex) xSemaphoreTake(g_mutex, portMAX_DELAY); }
  ~Lock() { if (g_mutex) xSemaphoreGive(g_mutex); }
};

uint32_t blob_crc(Blob& b) {
  const uint32_t saved = b.crc;
  b.crc = 0;
  const uint32_t crc = crc32_ieee(&b, sizeof(b));
  b.crc = saved;
  return crc;
}

bool save_locked() {
  g_blob->magic = kMagic;
  g_blob->version = kVersion;
  g_blob->size = sizeof(Blob);
  g_blob->crc = blob_crc(*g_blob);
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  const bool ok = prefs.putBytes(kKey, g_blob, sizeof(Blob)) == sizeof(Blob);
  prefs.end();
  return ok;
}

bool from_hex(const char* hex, uint8_t* out, size_t n) {
  if (hex == nullptr || strlen(hex) != n * 2) return false;
  for (size_t i = 0; i < n; ++i) {
    unsigned v = 0;
    if (sscanf(hex + i * 2, "%2x", &v) != 1) return false;
    out[i] = static_cast<uint8_t>(v);
  }
  return true;
}

void to_hex(const uint8_t* in, size_t n, char* out) {
  for (size_t i = 0; i < n; ++i) snprintf(out + i * 2, 3, "%02x", in[i]);
}

void set_defaults_locked() {
  *g_blob = Blob{};
  for (uint8_t u = 0; u < kUsers; ++u) {
    from_hex(kDefaults[u].salt, g_blob->users[u].salt, 16);
    from_hex(kDefaults[u].k_auth, g_blob->users[u].k_auth, 32);
    from_hex(kDefaults[u].k_enc, g_blob->users[u].k_enc, 32);
  }
}

bool equal_ct(const uint8_t* a, const uint8_t* b, size_t n) {
  uint8_t diff = 0;
  for (size_t i = 0; i < n; ++i) diff |= static_cast<uint8_t>(a[i] ^ b[i]);
  return diff == 0;
}

void hmac(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t len, uint8_t out[32]) {
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, key_len, msg, len, out);
}

void hmac_str(const uint8_t* key, size_t key_len, const char* msg, uint8_t out[32]) {
  hmac(key, key_len, reinterpret_cast<const uint8_t*>(msg), strlen(msg), out);
}

int find_user(const char* name) {
  for (uint8_t u = 0; u < kUsers; ++u) {
    if (name != nullptr && strcmp(name, kUserNames[u]) == 0) return u;
  }
  return -1;
}

// Single use: a matching live nonce is consumed whether or not the signature
// that follows is valid.
bool consume_nonce(const char* hex) {
  if (hex == nullptr || strlen(hex) != 32) return false;
  Lock lock;
  const uint32_t now = millis();
  for (size_t i = 0; i < kNonces; ++i) {
    Nonce& n = g_nonces[i];
    if (!n.live) continue;
    if (now - n.issued_ms >= kNonceTtlMs) {
      n.live = false;
      continue;
    }
    if (memcmp(n.hex, hex, 32) == 0) {
      n.live = false;
      return true;
    }
  }
  return false;
}

// The keystream and tag keys of a sealed payload.
void seal_keys(uint8_t user, uint8_t ks[32], uint8_t km[32]) {
  Lock lock;
  hmac_str(g_blob->users[user].k_enc, 32, "seal-stream", ks);
  hmac_str(g_blob->users[user].k_enc, 32, "seal-mac", km);
}

void keystream_xor(const uint8_t ks[32], const uint8_t iv[16], uint8_t* data, size_t len) {
  uint8_t block_in[20];
  memcpy(block_in, iv, 16);
  uint8_t block[32];
  for (uint32_t ctr = 0, off = 0; off < len; ++ctr, off += 32) {
    block_in[16] = static_cast<uint8_t>(ctr >> 24);
    block_in[17] = static_cast<uint8_t>(ctr >> 16);
    block_in[18] = static_cast<uint8_t>(ctr >> 8);
    block_in[19] = static_cast<uint8_t>(ctr);
    hmac(ks, 32, block_in, sizeof(block_in), block);
    for (size_t i = 0; i < 32 && off + i < len; ++i) data[off + i] ^= block[i];
  }
}

void seal_tag(const uint8_t km[32], const char* nonce, const uint8_t* iv, const uint8_t* ct, size_t len,
              uint8_t out[32]) {
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, km, 32);
  char head[48];
  const int n = snprintf(head, sizeof(head), "HPN1-seal\n%s\n", nonce);
  mbedtls_md_hmac_update(&ctx, reinterpret_cast<const uint8_t*>(head), n);
  mbedtls_md_hmac_update(&ctx, iv, 16);
  mbedtls_md_hmac_update(&ctx, ct, len);
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
}

bool open_signed(const char* msg, const char* sig_hex, JsonDocument& out, char* err, size_t err_size) {
  uint8_t mac[32];
  {
    Lock lock;
    hmac(reinterpret_cast<const uint8_t*>(g_blob->secret), strlen(g_blob->secret),
         reinterpret_cast<const uint8_t*>(msg), strlen(msg), mac);
  }
  uint8_t sig[32];
  if (!from_hex(sig_hex, sig, 32) || !equal_ct(mac, sig, 32)) {
    snprintf(err, err_size, "command signature invalid");
    return false;
  }
  if (deserializeJson(out, msg) != DeserializationError::Ok || !out.is<JsonObject>()) {
    snprintf(err, err_size, "msg is not a JSON object");
    return false;
  }
  const uint64_t seq = out["seq"] | 0ULL;
  Lock lock;
  if (seq == 0 || seq <= g_last_seq) {
    snprintf(err, err_size, "seq must increase (last %llu)", static_cast<unsigned long long>(g_last_seq));
    return false;
  }
  g_last_seq = seq;
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.putULong64(kSeqKey, seq);
    prefs.end();
  }
  return true;
}

}  // namespace

void begin() {
  if (g_mutex == nullptr) g_mutex = xSemaphoreCreateMutex();
  if (g_blob == nullptr) g_blob = new Blob();
  if (g_nonces == nullptr) g_nonces = new Nonce[kNonces]();
  Lock lock;
  bool ok = false;
  Preferences prefs;
  if (prefs.begin(kNamespace, true)) {
    if (prefs.getBytesLength(kKey) == sizeof(Blob) &&
        prefs.getBytes(kKey, g_blob, sizeof(Blob)) == sizeof(Blob)) {
      ok = g_blob->magic == kMagic && g_blob->version == kVersion && g_blob->size == sizeof(Blob) &&
           g_blob->crc == blob_crc(*g_blob) && strnlen(g_blob->secret, sizeof(g_blob->secret)) <= kSecretMax;
    }
    g_last_seq = prefs.getULong64(kSeqKey, 0);
    prefs.end();
  }
  if (!ok) set_defaults_locked();
  Serial.printf("[security] credentials %s, command secret %s\n",
                ok ? "stored" : "factory defaults", g_blob->secret[0] ? "set" : "not set");
}

void clear() {
  {
    Lock lock;
    set_defaults_locked();
    g_last_seq = 0;
  }
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.clear();
    prefs.end();
  }
}

bool reset_passwords_once() {
  char image[65];
  esp_app_get_elf_sha256(image, sizeof(image));
  Preferences prefs;
  if (!prefs.begin("hpnrst", false)) return false;
  const bool done = prefs.getString("image", "") == image;
  if (!done) {
    clear();
    prefs.putString("image", image);
  }
  prefs.end();
  return !done;
}

const char* user_name(uint8_t user) { return user < kUsers ? kUserNames[user] : "?"; }

void issue_nonce(char out[33]) {
  uint8_t raw[16];
  esp_fill_random(raw, sizeof(raw));
  to_hex(raw, sizeof(raw), out);
  Lock lock;
  const uint32_t now = millis();
  size_t slot = 0;
  uint32_t oldest = 0;
  for (size_t i = 0; i < kNonces; ++i) {
    if (!g_nonces[i].live || now - g_nonces[i].issued_ms >= kNonceTtlMs) {
      slot = i;
      break;
    }
    if (now - g_nonces[i].issued_ms > oldest) {
      oldest = now - g_nonces[i].issued_ms;
      slot = i;
    }
  }
  memcpy(g_nonces[slot].hex, out, 33);
  g_nonces[slot].issued_ms = now;
  g_nonces[slot].live = true;
}

bool challenge(const char* user, JsonObject out) {
  const int u = find_user(user);
  if (u < 0) return false;
  char salt[33];
  {
    Lock lock;
    to_hex(g_blob->users[u].salt, 16, salt);
  }
  char nonce[33];
  issue_nonce(nonce);
  out["user"] = kUserNames[u];
  out["salt"] = salt;
  out["iter"] = kIterations;
  out["nonce"] = nonce;
  out["ttl"] = kNonceTtlMs / 1000;
  return true;
}

Auth verify(const char* auth_header, const char* method, const char* path, const char* canonical_query,
            const char* body_hash_hex) {
  Auth a{};
  a.status = 401;
  a.reason = "header";
  if (auth_header == nullptr) return a;
  // user=<name>,nonce=<hex32>,sig=<hex64>
  char user[12] = {}, sig_hex[65] = {};
  const char* p = strstr(auth_header, "user=");
  const char* n = strstr(auth_header, "nonce=");
  const char* s = strstr(auth_header, "sig=");
  if (p == nullptr || n == nullptr || s == nullptr) return a;
  sscanf(p + 5, "%11[^,]", user);
  sscanf(n + 6, "%32[0-9a-f]", a.nonce);
  sscanf(s + 4, "%64[0-9a-f]", sig_hex);
  const int u = find_user(user);
  if (u < 0) {
    a.reason = "user";
    return a;
  }
  a.user = static_cast<uint8_t>(u);
  {
    Lock lock;
    if (g_locked_until[u] != 0 && static_cast<int32_t>(millis() - g_locked_until[u]) < 0) {
      a.status = 423;
      a.reason = "locked";
      return a;
    }
  }
  if (!consume_nonce(a.nonce)) {
    a.reason = "nonce";
    return a;
  }
  String text = "HPN1\n";
  text += method;
  text += '\n';
  text += path;
  text += '\n';
  text += canonical_query;
  text += '\n';
  text += a.nonce;
  text += '\n';
  text += body_hash_hex;
  uint8_t mac[32], sig[32];
  {
    Lock lock;
    hmac_str(g_blob->users[u].k_auth, 32, text.c_str(), mac);
  }
  if (!from_hex(sig_hex, sig, 32) || !equal_ct(mac, sig, 32)) {
    a.reason = "sig";
    Lock lock;
    if (++g_failures[u] >= kMaxFailures) {
      g_failures[u] = 0;
      g_locked_until[u] = millis() + kLockMs;
    }
    return a;
  }
  {
    Lock lock;
    g_failures[u] = 0;
    g_locked_until[u] = 0;
  }
  a.role = kUserRoles[u];
  a.status = 0;
  a.reason = "";
  return a;
}

bool unseal(uint8_t user, const char* nonce, const char* b64, char* out, size_t out_size, size_t* out_len) {
  if (user >= kUsers || b64 == nullptr) return false;
  size_t raw_len = 0;
  const size_t in_len = strlen(b64);
  if (mbedtls_base64_decode(reinterpret_cast<uint8_t*>(out), out_size, &raw_len,
                            reinterpret_cast<const uint8_t*>(b64), in_len) != 0 ||
      raw_len < 48) {
    return false;
  }
  uint8_t* raw = reinterpret_cast<uint8_t*>(out);
  const size_t ct_len = raw_len - 48;
  uint8_t ks[32], km[32], tag[32];
  seal_keys(user, ks, km);
  seal_tag(km, nonce, raw, raw + 16, ct_len, tag);
  if (!equal_ct(tag, raw + 16 + ct_len, 32)) return false;
  uint8_t iv[16];
  memcpy(iv, raw, 16);
  memmove(raw, raw + 16, ct_len);
  keystream_xor(ks, iv, raw, ct_len);
  if (ct_len >= out_size) return false;
  out[ct_len] = '\0';
  if (out_len) *out_len = ct_len;
  return true;
}

String seal(uint8_t user, const char* nonce, const char* plaintext, size_t len) {
  if (user >= kUsers) return String();
  std::unique_ptr<uint8_t[]> raw(new (std::nothrow) uint8_t[len + 48]);
  if (!raw) return String();
  esp_fill_random(raw.get(), 16);
  memcpy(raw.get() + 16, plaintext, len);
  uint8_t ks[32], km[32];
  seal_keys(user, ks, km);
  keystream_xor(ks, raw.get(), raw.get() + 16, len);
  seal_tag(km, nonce, raw.get(), raw.get() + 16, len, raw.get() + 16 + len);
  size_t b64_len = 0;
  mbedtls_base64_encode(nullptr, 0, &b64_len, raw.get(), len + 48);
  std::unique_ptr<uint8_t[]> b64(new (std::nothrow) uint8_t[b64_len + 1]);
  if (!b64) return String();
  mbedtls_base64_encode(b64.get(), b64_len + 1, &b64_len, raw.get(), len + 48);
  b64[b64_len] = '\0';
  return String(reinterpret_cast<const char*>(b64.get()));
}

bool set_user_keys(const char* user, const char* salt_hex, const char* k_auth_hex, const char* k_enc_hex,
                   char* err, size_t err_size) {
  err[0] = '\0';
  const int u = find_user(user);
  if (u < 0) {
    snprintf(err, err_size, "user must be admin or monitor");
    return false;
  }
  UserKeys k{};
  if (!from_hex(salt_hex, k.salt, 16) || !from_hex(k_auth_hex, k.k_auth, 32) ||
      !from_hex(k_enc_hex, k.k_enc, 32)) {
    snprintf(err, err_size, "salt (32 hex), k_auth and k_enc (64 hex) required");
    return false;
  }
  bool saved = false;
  {
    Lock lock;
    g_blob->users[u] = k;
    saved = save_locked();
  }
  char detail[64];
  snprintf(detail, sizeof(detail), "Password of %s changed", kUserNames[u]);
  web::log_event(saved ? 0 : 1, 1610, saved ? detail : "Password change not saved");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

bool set_command_secret(const char* secret, char* err, size_t err_size) {
  err[0] = '\0';
  const size_t len = secret ? strlen(secret) : 0;
  if (len != 0 && (len < kSecretMin || len > kSecretMax)) {
    snprintf(err, err_size, "secret must be %u-%u characters", static_cast<unsigned>(kSecretMin),
             static_cast<unsigned>(kSecretMax));
    return false;
  }
  bool saved = false;
  {
    Lock lock;
    snprintf(g_blob->secret, sizeof(g_blob->secret), "%s", len ? secret : "");
    saved = save_locked();
  }
  web::log_event(saved ? 0 : 1, 1611, len ? "Command secret set" : "Command secret removed");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

bool command_secret_set() {
  Lock lock;
  return g_blob->secret[0] != '\0';
}

bool open_command(const char* payload, JsonDocument& out, char* err, size_t err_size) {
  JsonDocument env;
  if (deserializeJson(env, payload) != DeserializationError::Ok) {
    snprintf(err, err_size, "invalid JSON");
    return false;
  }
  return open_command(env.as<JsonVariantConst>(), out, err, err_size);
}

bool open_command(JsonVariantConst in, JsonDocument& out, char* err, size_t err_size) {
  err[0] = '\0';
  const char* msg = in["msg"] | static_cast<const char*>(nullptr);
  const char* sig = in["sig"] | static_cast<const char*>(nullptr);
  if (command_secret_set()) {
    if (msg == nullptr || sig == nullptr) {
      snprintf(err, err_size, "signed envelope {msg, sig} required");
      return false;
    }
    return open_signed(msg, sig, out, err, err_size);
  }
  // No secret yet: plain JSON (or the msg of an envelope) is accepted.
  if (msg != nullptr) {
    if (deserializeJson(out, msg) != DeserializationError::Ok) {
      snprintf(err, err_size, "msg is not JSON");
      return false;
    }
    return true;
  }
  out.set(in);
  return true;
}

void sha256_hex(const uint8_t* data, size_t len, char out[65]) {
  uint8_t h[32];
  mbedtls_sha256(data, len, h, 0);
  to_hex(h, 32, out);
}

void status_json(JsonObject out) {
  char fp[17] = {};
  uint64_t seq;
  bool secret;
  bool defaults[kUsers];
  {
    Lock lock;
    secret = g_blob->secret[0] != '\0';
    if (secret) {
      // Lets the observer side check that both hold the same secret.
      uint8_t h[32];
      mbedtls_sha256(reinterpret_cast<const uint8_t*>(g_blob->secret), strlen(g_blob->secret), h, 0);
      to_hex(h, 8, fp);
    }
    seq = g_last_seq;
    for (uint8_t u = 0; u < kUsers; ++u) {
      uint8_t k[32];
      from_hex(kDefaults[u].k_auth, k, 32);
      defaults[u] = memcmp(k, g_blob->users[u].k_auth, 32) == 0;
    }
  }
  JsonObject users = out["users"].to<JsonObject>();
  for (uint8_t u = 0; u < kUsers; ++u) {
    JsonObject o = users[kUserNames[u]].to<JsonObject>();
    o["role"] = kUserRoles[u] == ROLE_ADMIN ? "admin" : "read-only";
    o["defaultPassword"] = defaults[u];
  }
  JsonObject cmd = out["commands"].to<JsonObject>();
  cmd["secretSet"] = secret;
  cmd["fingerprint"] = fp;
  cmd["lastSeq"] = seq;
}

}  // namespace security
}  // namespace hpn
