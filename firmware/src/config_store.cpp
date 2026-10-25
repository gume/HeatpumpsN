#include "hpn/config_store.h"

#include <Arduino.h>
#include <Preferences.h>
#include <stddef.h>
#include <string.h>

namespace hpn {
namespace {

constexpr const char* kNamespace = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"
constexpr uint16_t kVersion = 2;
constexpr uint16_t kVersionV1 = 1;

struct ConfigBlobHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
};

struct ConfigBlob {
  ConfigBlobHeader header;
  Config config;
};

// v1 ended with floor_pause_duration_s. New fields are append-only, so the
// old Config is exactly the prefix before hostname.
constexpr size_t kConfigV1Size = offsetof(Config, hostname);
constexpr size_t kConfigV1BlobSize = sizeof(ConfigBlobHeader) + kConfigV1Size;

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
  bool migrated = false;

  // Read into a zeroed v2 blob. For a v1 blob only the old prefix is filled;
  // the appended fields stay zero and are then replaced by compiled defaults.
  if (stored >= sizeof(ConfigBlobHeader) && stored <= sizeof(ConfigBlob)) {
    ConfigBlob blob{};
    if (g_prefs.getBytes(kKey, &blob, stored) == stored && blob.header.magic == kMagic) {
      if (blob.header.version == kVersion && blob.header.size == sizeof(Config) &&
          stored == sizeof(ConfigBlob)) {
        config = blob.config;
        ok = true;
      } else if (blob.header.version == kVersionV1 && blob.header.size == kConfigV1Size &&
                 stored == kConfigV1BlobSize) {
        Config upgraded = config_defaults();
        memcpy(&upgraded, &blob.config, kConfigV1Size);
        config = upgraded;
        ok = true;
        migrated = true;
      }
    }
  }

  g_prefs.end();

  if (ok) {
    Serial.println(migrated ? "[config] migrated saved v1 configuration"
                            : "[config] loaded saved configuration from NVS");
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
  blob.header.magic = kMagic;
  blob.header.version = kVersion;
  blob.header.size = sizeof(Config);
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
