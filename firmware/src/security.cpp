#include "hpn/security.h"

#include <Arduino.h>
#include <Preferences.h>
#include <WebAuthentication.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <string.h>

#include "hpn/crc32.h"
#include "hpn/web_portal.h"

namespace hpn {
namespace security {
namespace {

constexpr const char* kNamespace = "hpnsec";
constexpr const char* kKey = "blob";
constexpr const char* kSeqKey = "seq";
constexpr uint32_t kMagic = 0x48504E53u;  // "HPNS"
constexpr uint16_t kVersion = 2;           // no migration: another version loads the defaults
constexpr const char* kRealm = "HeatpumpsN";
constexpr const char* kUser = "admin";
constexpr const char* kDefaultPassword = "admin";
constexpr size_t kSecretMin = 16;
constexpr size_t kSecretMax = 64;
constexpr uint32_t kMaxUploadMinutes = 60;

struct Blob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  char ha1[33];
  char secret[kSecretMax + 1];
};

SemaphoreHandle_t g_mutex = nullptr;
Blob g_blob{};
uint64_t g_last_seq = 0;
uint32_t g_upload_until_ms = 0;
bool g_upload_open = false;
char g_ws_token[33] = {};

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
  g_blob.magic = kMagic;
  g_blob.version = kVersion;
  g_blob.size = sizeof(Blob);
  g_blob.crc = blob_crc(g_blob);
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  const bool ok = prefs.putBytes(kKey, &g_blob, sizeof(g_blob)) == sizeof(g_blob);
  prefs.end();
  return ok;
}

void set_defaults_locked() {
  g_blob = Blob{};
  const String ha1 = generateDigestHash(kUser, kDefaultPassword, kRealm);
  snprintf(g_blob.ha1, sizeof(g_blob.ha1), "%s", ha1.c_str());
}

void to_hex(const uint8_t* in, size_t n, char* out) {
  for (size_t i = 0; i < n; ++i) snprintf(out + i * 2, 3, "%02x", in[i]);
}

// Constant-time comparison of two equal-length strings.
bool equal_ct(const char* a, const char* b, size_t n) {
  uint8_t diff = 0;
  for (size_t i = 0; i < n; ++i) diff |= static_cast<uint8_t>(a[i] ^ b[i]);
  return diff == 0;
}

bool open_signed(const char* msg, const char* sig_hex, JsonDocument& out, char* err, size_t err_size) {
  uint8_t mac[32];
  {
    Lock lock;
    const mbedtls_md_info_t* md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    mbedtls_md_hmac(md, reinterpret_cast<const unsigned char*>(g_blob.secret), strlen(g_blob.secret),
                    reinterpret_cast<const unsigned char*>(msg), strlen(msg), mac);
  }
  char expect[65];
  to_hex(mac, sizeof(mac), expect);
  if (strlen(sig_hex) != 64 || !equal_ct(expect, sig_hex, 64)) {
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
  Lock lock;
  bool ok = false;
  Preferences prefs;
  if (prefs.begin(kNamespace, true)) {
    if (prefs.getBytesLength(kKey) == sizeof(Blob) &&
        prefs.getBytes(kKey, &g_blob, sizeof(g_blob)) == sizeof(g_blob)) {
      ok = g_blob.magic == kMagic && g_blob.version == kVersion && g_blob.size == sizeof(Blob) &&
           g_blob.crc == blob_crc(g_blob) && strnlen(g_blob.ha1, sizeof(g_blob.ha1)) == 32 &&
           strnlen(g_blob.secret, sizeof(g_blob.secret)) <= kSecretMax;
    }
    g_last_seq = prefs.getULong64(kSeqKey, 0);
    prefs.end();
  }
  if (!ok) set_defaults_locked();
  for (size_t i = 0; i < 16; ++i) {
    snprintf(g_ws_token + i * 2, 3, "%02x", static_cast<unsigned>(esp_random() & 0xFF));
  }
  Serial.printf("[security] login %s, command secret %s, uploads closed\n",
                ok ? "stored" : "default (admin/admin)", g_blob.secret[0] ? "set" : "not set");
}

void clear() {
  {
    Lock lock;
    set_defaults_locked();
    g_last_seq = 0;
    g_upload_open = false;
  }
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.clear();
    prefs.end();
  }
}

const char* realm() { return kRealm; }
const char* username() { return kUser; }

String digest_ha1() {
  Lock lock;
  return String(g_blob.ha1);
}

bool set_password(const char* current, const char* next, char* err, size_t err_size) {
  err[0] = '\0';
  const String cur = generateDigestHash(kUser, current ? current : "", kRealm);
  const size_t len = next ? strlen(next) : 0;
  if (len < 4 || len > 63) {
    snprintf(err, err_size, "new password must be 4-63 characters");
    return false;
  }
  bool saved = false;
  {
    Lock lock;
    if (!equal_ct(cur.c_str(), g_blob.ha1, 32)) {
      snprintf(err, err_size, "current password is wrong");
      return false;
    }
    const String ha1 = generateDigestHash(kUser, next, kRealm);
    snprintf(g_blob.ha1, sizeof(g_blob.ha1), "%s", ha1.c_str());
    saved = save_locked();
  }
  web::log_event(saved ? 0 : 1, 1610, saved ? "Portal password changed" : "Portal password not saved");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

const char* ws_token() { return g_ws_token; }

bool check_ws_token(const char* token) {
  return token != nullptr && strlen(token) == 32 && equal_ct(token, g_ws_token, 32);
}

bool uploads_open() {
  Lock lock;
  if (g_upload_open && static_cast<int32_t>(millis() - g_upload_until_ms) >= 0) g_upload_open = false;
  return g_upload_open;
}

void open_uploads(uint32_t minutes) {
  if (minutes > kMaxUploadMinutes) minutes = kMaxUploadMinutes;
  {
    Lock lock;
    g_upload_open = minutes > 0;
    g_upload_until_ms = millis() + minutes * 60000u;
  }
  char detail[64];
  snprintf(detail, sizeof(detail), minutes ? "Firmware uploads opened for %u min" : "Firmware uploads closed",
           static_cast<unsigned>(minutes));
  web::log_event(minutes ? 1 : 0, 1612, detail);
}

uint32_t uploads_remaining_s() {
  if (!uploads_open()) return 0;
  Lock lock;
  const int32_t left = static_cast<int32_t>(g_upload_until_ms - millis());
  return left > 0 ? static_cast<uint32_t>(left) / 1000u : 0;
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
    snprintf(g_blob.secret, sizeof(g_blob.secret), "%s", len ? secret : "");
    saved = save_locked();
  }
  web::log_event(saved ? 0 : 1, 1611, len ? "Command secret set" : "Command secret removed");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

bool command_secret_set() {
  Lock lock;
  return g_blob.secret[0] != '\0';
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

void status_json(JsonObject out) {
  char fp[17] = {};
  uint64_t seq;
  bool secret;
  {
    Lock lock;
    secret = g_blob.secret[0] != '\0';
    if (secret) {
      // Lets the observer side check that both hold the same secret.
      uint8_t h[32];
      mbedtls_sha256(reinterpret_cast<const unsigned char*>(g_blob.secret), strlen(g_blob.secret), h, 0);
      to_hex(h, 8, fp);
    }
    seq = g_last_seq;
  }
  const String def = generateDigestHash(kUser, kDefaultPassword, kRealm);
  out["user"] = kUser;
  out["defaultPassword"] = digest_ha1() == def;
  JsonObject up = out["uploads"].to<JsonObject>();
  up["open"] = uploads_open();
  up["remainingS"] = uploads_remaining_s();
  JsonObject cmd = out["commands"].to<JsonObject>();
  cmd["secretSet"] = secret;
  cmd["fingerprint"] = fp;
  cmd["lastSeq"] = seq;
}

}  // namespace security
}  // namespace hpn
