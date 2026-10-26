#include "hpn/config_store.h"

#include <Arduino.h>
#include <Preferences.h>
#include <stddef.h>
#include <string.h>

#include "hpn/crc32.h"

namespace hpn {
namespace {

constexpr const char* kNamespace = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"
constexpr uint16_t kVersion = 1;

struct ConfigBlobHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
};

struct ConfigBlobPrefix {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
};

struct ConfigBlob {
  ConfigBlobHeader header;
  Config config;
};

static_assert(sizeof(ConfigBlobHeader) == 12, "unexpected NVS header layout");
static_assert(sizeof(ConfigBlobPrefix) == 8, "unexpected NVS prefix layout");

Preferences g_prefs;

bool open_store() { return g_prefs.begin(kNamespace, false); }

uint32_t blob_crc(ConfigBlob& blob) {
  const uint32_t saved = blob.header.crc;
  blob.header.crc = 0;
  const uint32_t crc = crc32_ieee(&blob, sizeof(blob));
  blob.header.crc = saved;
  return crc;
}

}  // namespace

ConfigLoadResult config_load(Config& config) {
  if (!open_store()) {
    Serial.println("[config] NVS open failed; using compiled defaults");
    return ConfigLoadResult::Corrupt;
  }

  const size_t stored = g_prefs.getBytesLength(kKey);
  ConfigLoadResult result = ConfigLoadResult::Missing;

  if (stored != 0) {
    // Preferences::getBytes() requires a buffer large enough for the whole
    // blob, so a short prefix read cannot be used to sniff the format. The
    // current format is the largest one (fields were only ever removed from
    // older versions), so anything that still fits is read in full.
    if (stored < sizeof(ConfigBlobPrefix)) {
      result = ConfigLoadResult::Corrupt;
    } else if (stored > sizeof(ConfigBlob)) {
      // Larger than any recorded format: an old/foreign blob, not damage.
      result = ConfigLoadResult::Stale;
    } else {
      ConfigBlob blob{};
      if (g_prefs.getBytes(kKey, &blob, stored) != stored) {
        result = ConfigLoadResult::Corrupt;
      } else if (blob.header.magic != kMagic || blob.header.version != kVersion) {
        // Old-format or foreign blob. No migration: discard it and use defaults.
        result = ConfigLoadResult::Stale;
      } else if (stored != sizeof(ConfigBlob) || blob.header.size != sizeof(Config)) {
        result = ConfigLoadResult::Corrupt;
      } else if (blob.header.crc == blob_crc(blob)) {
        config = blob.config;
        result = ConfigLoadResult::Loaded;
      } else {
        result = ConfigLoadResult::Corrupt;
      }
    }
  }

  g_prefs.end();

  switch (result) {
    case ConfigLoadResult::Loaded:
      Serial.println("[config] loaded saved configuration from NVS");
      break;
    case ConfigLoadResult::Stale:
      Serial.printf("[config] saved configuration format differs (%u bytes); using compiled defaults\n",
                    static_cast<unsigned>(stored));
      break;
    case ConfigLoadResult::Corrupt:
      Serial.printf("[config] NVS CRC/format check failed (%u bytes); using compiled defaults\n",
                    static_cast<unsigned>(stored));
      break;
    case ConfigLoadResult::Missing:
    default:
      Serial.println("[config] no saved configuration; using compiled defaults");
      break;
  }

  return result;
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
  blob.header.crc = 0;
  blob.header.crc = blob_crc(blob);

  // A second pass verifies that the checksum helper is deterministic before
  // the blob is committed.
  if (blob.header.crc != blob_crc(blob)) {
    g_prefs.end();
    Serial.println("[config] internal CRC error; settings not saved");
    return false;
  }

  const size_t written = g_prefs.putBytes(kKey, &blob, sizeof(blob));
  g_prefs.end();

  const bool ok = written == sizeof(blob);
  Serial.printf("[config] save %s (%u bytes, crc %08lx)\n", ok ? "ok" : "FAILED",
                static_cast<unsigned>(written),
                static_cast<unsigned long>(blob.header.crc));
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
