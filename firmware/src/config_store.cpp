#include "hpn/config_store.h"

#include <Arduino.h>
#include <Preferences.h>

namespace hpn {
namespace {

constexpr const char* kNamespace = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"
constexpr uint16_t kVersion = 1;

struct ConfigBlob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  Config config;
};

Preferences g_prefs;

bool open_store() { return g_prefs.begin(kNamespace, false); }

}  // namespace

bool config_load(Config& config) {
  if (!open_store()) {
    Serial.println("[config] NVS open failed; using compiled defaults");
    return false;
  }

  const size_t stored = g_prefs.getBytesLength(kKey);
  bool ok = false;

  if (stored == sizeof(ConfigBlob)) {
    ConfigBlob blob{};
    if (g_prefs.getBytes(kKey, &blob, sizeof(blob)) == sizeof(blob) && blob.magic == kMagic &&
        blob.version == kVersion && blob.size == sizeof(Config)) {
      config = blob.config;
      ok = true;
    }
  }

  g_prefs.end();

  if (ok) {
    Serial.println("[config] loaded saved configuration from NVS");
  } else if (stored != 0) {
    Serial.printf("[config] ignoring invalid NVS blob (%u bytes)\n", static_cast<unsigned>(stored));
  } else {
    Serial.println("[config] no saved configuration; using compiled defaults");
  }

  return ok;
}

bool config_save(const Config& config) {
  if (!open_store()) {
    Serial.println("[config] NVS open failed; settings not saved");
    return false;
  }

  ConfigBlob blob{};
  blob.magic = kMagic;
  blob.version = kVersion;
  blob.size = sizeof(Config);
  blob.config = config;

  const size_t written = g_prefs.putBytes(kKey, &blob, sizeof(blob));
  g_prefs.end();

  const bool ok = written == sizeof(blob);
  Serial.printf("[config] save %s (%u bytes)\n", ok ? "ok" : "FAILED",
                static_cast<unsigned>(written));
  return ok;
}

void config_clear() {
  if (!open_store()) {
    return;
  }
  g_prefs.remove(kKey);
  g_prefs.end();
  Serial.println("[config] saved configuration cleared");
}

}  // namespace hpn
