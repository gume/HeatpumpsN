#include "hpn/security.h"

#include <Arduino.h>
#include <Preferences.h>
#include <WebAuthentication.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/base64.h>
#include <mbedtls/pk.h>
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
constexpr uint16_t kVersion = 1;
constexpr const char* kRealm = "HeatpumpsN";
constexpr const char* kUser = "admin";
constexpr const char* kDefaultPassword = "admin";
constexpr size_t kPemMax = 256;          // a P-256 public key PEM is ~180 bytes
constexpr uint32_t kMaxUnsignedMinutes = 60;

struct Blob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  char ha1[33];
  char pem[KEY_COUNT][kPemMax];
};

SemaphoreHandle_t g_mutex = nullptr;
// On the heap: the ESP32-S2 has no static DRAM to spare for the two PEMs.
Blob* g_blob = nullptr;
mbedtls_pk_context g_pk[KEY_COUNT];
bool g_pk_ok[KEY_COUNT] = {};
uint64_t g_last_seq = 0;
uint32_t g_unsigned_until_ms = 0;
bool g_unsigned = false;
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

void set_defaults_locked() {
  *g_blob = Blob{};
  const String ha1 = generateDigestHash(kUser, kDefaultPassword, kRealm);
  snprintf(g_blob->ha1, sizeof(g_blob->ha1), "%s", ha1.c_str());
}

// Parse a PEM public key into `ctx`; only ECDSA P-256 is accepted.
bool parse_pem(mbedtls_pk_context& ctx, const char* pem) {
  mbedtls_pk_free(&ctx);
  mbedtls_pk_init(&ctx);
  if (pem == nullptr || pem[0] == '\0') return false;
  const int rc = mbedtls_pk_parse_public_key(&ctx, reinterpret_cast<const unsigned char*>(pem),
                                             strlen(pem) + 1);
  return rc == 0 && mbedtls_pk_can_do(&ctx, MBEDTLS_PK_ECDSA) && mbedtls_pk_get_bitlen(&ctx) == 256;
}

// Fingerprint as `openssl ec -pubin -outform DER | sha256sum` prints it (first 16 hex).
void fingerprint(const char* pem, char* out, size_t out_size) {
  out[0] = '\0';
  if (pem == nullptr || pem[0] == '\0') return;
  char body[kPemMax];
  size_t n = 0;
  const char* p = strstr(pem, "-----BEGIN PUBLIC KEY-----");
  const char* e = strstr(pem, "-----END PUBLIC KEY-----");
  if (p == nullptr || e == nullptr) return;
  for (p += strlen("-----BEGIN PUBLIC KEY-----"); p < e && n < sizeof(body) - 1; ++p) {
    if (*p != '\n' && *p != '\r' && *p != ' ') body[n++] = *p;
  }
  body[n] = '\0';
  uint8_t der[160];
  size_t der_len = 0;
  if (mbedtls_base64_decode(der, sizeof(der), &der_len, reinterpret_cast<const unsigned char*>(body), n) != 0) {
    return;
  }
  uint8_t hash[32];
  mbedtls_sha256(der, der_len, hash, 0);
  for (size_t i = 0; i < 8 && (i * 2 + 2) < out_size; ++i) snprintf(out + i * 2, 3, "%02x", hash[i]);
}

bool open_signed(const char* msg, size_t msg_len, const char* sig_b64, JsonDocument& out, char* err,
                 size_t err_size) {
  uint8_t sig[96];
  const size_t sig_len = base64_decode(sig_b64, sig, sizeof(sig));
  if (sig_len == 0) {
    snprintf(err, err_size, "sig is not valid base64");
    return false;
  }
  uint8_t hash[32];
  mbedtls_sha256(reinterpret_cast<const unsigned char*>(msg), msg_len, hash, 0);
  if (!verify(KEY_OBSERVER, hash, sig, sig_len)) {
    snprintf(err, err_size, "observer signature invalid");
    return false;
  }
  if (deserializeJson(out, msg, msg_len) != DeserializationError::Ok || !out.is<JsonObject>()) {
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
  for (auto& ctx : g_pk) mbedtls_pk_init(&ctx);
  Lock lock;
  bool ok = false;
  Preferences prefs;
  if (prefs.begin(kNamespace, true)) {
    if (prefs.getBytesLength(kKey) == sizeof(Blob) &&
        prefs.getBytes(kKey, g_blob, sizeof(Blob)) == sizeof(Blob)) {
      ok = g_blob->magic == kMagic && g_blob->version == kVersion && g_blob->size == sizeof(Blob) &&
           g_blob->crc == blob_crc(*g_blob) && g_blob->ha1[32] == '\0' && strlen(g_blob->ha1) == 32;
    }
    g_last_seq = prefs.getULong64(kSeqKey, 0);
    prefs.end();
  }
  if (!ok) set_defaults_locked();
  for (uint8_t i = 0; i < KEY_COUNT; ++i) {
    g_blob->pem[i][kPemMax - 1] = '\0';
    g_pk_ok[i] = parse_pem(g_pk[i], g_blob->pem[i]);
  }
  for (size_t i = 0; i < 16; ++i) {
    snprintf(g_ws_token + i * 2, 3, "%02x", static_cast<unsigned>(esp_random() & 0xFF));
  }
  Serial.printf("[security] login %s, OTA key %s, observer key %s\n",
                ok ? "stored" : "default (admin/admin)", g_pk_ok[KEY_OTA] ? "installed" : "none",
                g_pk_ok[KEY_OBSERVER] ? "installed" : "none");
}

void clear() {
  {
    Lock lock;
    set_defaults_locked();
    for (uint8_t i = 0; i < KEY_COUNT; ++i) g_pk_ok[i] = parse_pem(g_pk[i], "");
    g_last_seq = 0;
    g_unsigned = false;
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
  return String(g_blob->ha1);
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
    if (strcmp(cur.c_str(), g_blob->ha1) != 0) {
      snprintf(err, err_size, "current password is wrong");
      return false;
    }
    const String ha1 = generateDigestHash(kUser, next, kRealm);
    snprintf(g_blob->ha1, sizeof(g_blob->ha1), "%s", ha1.c_str());
    saved = save_locked();
  }
  web::log_event(saved ? 0 : 1, 1610, saved ? "Portal password changed" : "Portal password not saved");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

const char* ws_token() { return g_ws_token; }

bool check_ws_token(const char* token) {
  if (token == nullptr || strlen(token) != 32) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < 32; ++i) diff |= static_cast<uint8_t>(token[i] ^ g_ws_token[i]);
  return diff == 0;
}

bool set_key(KeySlot slot, const char* pem, char* err, size_t err_size) {
  err[0] = '\0';
  if (slot >= KEY_COUNT) return false;
  const size_t len = pem ? strlen(pem) : 0;
  if (len >= kPemMax) {
    snprintf(err, err_size, "key too long (PEM public key expected)");
    return false;
  }
  if (len > 0) {
    mbedtls_pk_context probe;
    mbedtls_pk_init(&probe);
    const bool valid = parse_pem(probe, pem);
    mbedtls_pk_free(&probe);
    if (!valid) {
      snprintf(err, err_size, "not an ECDSA P-256 public key in PEM format");
      return false;
    }
  }
  bool saved = false;
  {
    Lock lock;
    snprintf(g_blob->pem[slot], kPemMax, "%s", len ? pem : "");
    g_pk_ok[slot] = parse_pem(g_pk[slot], g_blob->pem[slot]);
    saved = save_locked();
  }
  char detail[80];
  snprintf(detail, sizeof(detail), "%s public key %s", slot == KEY_OTA ? "OTA" : "Observer",
           len ? "installed" : "removed");
  web::log_event(saved ? 0 : 1, 1611, detail);
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

bool key_installed(KeySlot slot) {
  Lock lock;
  return slot < KEY_COUNT && g_pk_ok[slot];
}

bool verify(KeySlot slot, const uint8_t hash[32], const uint8_t* sig, size_t sig_len) {
  Lock lock;
  if (slot >= KEY_COUNT || !g_pk_ok[slot]) return false;
  return mbedtls_pk_verify(&g_pk[slot], MBEDTLS_MD_SHA256, hash, 32, sig, sig_len) == 0;
}

size_t base64_decode(const char* in, uint8_t* out, size_t out_size) {
  if (in == nullptr) return 0;
  size_t len = 0;
  if (mbedtls_base64_decode(out, out_size, &len, reinterpret_cast<const unsigned char*>(in), strlen(in)) != 0) {
    return 0;
  }
  return len;
}

bool parse_hex32(const char* hex, uint8_t out[32]) {
  if (hex == nullptr || strlen(hex) != 64) return false;
  for (size_t i = 0; i < 32; ++i) {
    unsigned v = 0;
    if (sscanf(hex + i * 2, "%2x", &v) != 1) return false;
    out[i] = static_cast<uint8_t>(v);
  }
  return true;
}

bool ota_signature_required() {
  Lock lock;
  if (g_unsigned && static_cast<int32_t>(millis() - g_unsigned_until_ms) >= 0) g_unsigned = false;
  return g_pk_ok[KEY_OTA] && !g_unsigned;
}

void allow_unsigned(uint32_t minutes) {
  if (minutes > kMaxUnsignedMinutes) minutes = kMaxUnsignedMinutes;
  {
    Lock lock;
    g_unsigned = minutes > 0;
    g_unsigned_until_ms = millis() + minutes * 60000u;
  }
  char detail[80];
  snprintf(detail, sizeof(detail), minutes ? "Unsigned updates allowed for %u min" : "Update signatures enforced",
           static_cast<unsigned>(minutes));
  web::log_event(minutes ? 1 : 0, 1612, detail);
}

uint32_t unsigned_remaining_s() {
  Lock lock;
  if (!g_unsigned) return 0;
  const int32_t left = static_cast<int32_t>(g_unsigned_until_ms - millis());
  return left > 0 ? static_cast<uint32_t>(left) / 1000u : 0;
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
  if (key_installed(KEY_OBSERVER)) {
    if (msg == nullptr || sig == nullptr) {
      snprintf(err, err_size, "signed envelope {msg, sig} required");
      return false;
    }
    return open_signed(msg, strlen(msg), sig, out, err, err_size);
  }
  // No observer key yet: plain JSON (or the msg of an envelope) is accepted.
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
  char fp[2][24];
  bool installed[KEY_COUNT];
  uint64_t seq;
  {
    Lock lock;
    for (uint8_t i = 0; i < KEY_COUNT; ++i) {
      fingerprint(g_blob->pem[i], fp[i], sizeof(fp[i]));
      installed[i] = g_pk_ok[i];
    }
    seq = g_last_seq;
  }
  const String def = generateDigestHash(kUser, kDefaultPassword, kRealm);
  out["user"] = kUser;
  out["defaultPassword"] = digest_ha1() == def;
  JsonObject ota = out["ota"].to<JsonObject>();
  ota["keyInstalled"] = installed[KEY_OTA];
  ota["fingerprint"] = fp[KEY_OTA];
  ota["signatureRequired"] = ota_signature_required();
  ota["unsignedRemainingS"] = unsigned_remaining_s();
  JsonObject obs = out["observer"].to<JsonObject>();
  obs["keyInstalled"] = installed[KEY_OBSERVER];
  obs["fingerprint"] = fp[KEY_OBSERVER];
  obs["lastSeq"] = seq;
}

}  // namespace security
}  // namespace hpn
