#include "hpn/config_store.h"

#include <Arduino.h>
#include <Preferences.h>
#include <math.h>
#include <stddef.h>
#include <string.h>

#include "hpn/crc32.h"

namespace hpn {
namespace {

constexpr const char* kNamespace = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"
constexpr uint16_t kVersion = 3;

// v1 blobs contain the unchanged 1288-byte Config prefix (the first version
// already used the former tail padding for mix_max_open_pct). v2 appended the
// pump-calibration, operating-window, overshoot, sensor-role and inferred-DHW
// fields. v3 appends the output-driver/port-expander block. Each migration
// starts from current defaults and copies the unchanged old prefix, so the new
// fields receive a valid value.
constexpr size_t kV1ConfigSize = 1288;
constexpr size_t kV2ConfigSize = 1348;
constexpr size_t kCurrentConfigSize = 1364;

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
static_assert(sizeof(Config) > kV2ConfigSize,
              "v3 fields must be appended after the v2 Config prefix");
// Any future field addition must bump kVersion and extend the migration path
// instead of silently invalidating stored settings.
static_assert(sizeof(Config) == kCurrentConfigSize,
              "Config layout changed; review the NVS format");

Preferences g_prefs;

bool open_store() { return g_prefs.begin(kNamespace, false); }

uint32_t blob_crc_bytes(uint8_t* bytes, size_t size) {
  ConfigBlobHeader* header = reinterpret_cast<ConfigBlobHeader*>(bytes);
  const uint32_t saved = header->crc;
  header->crc = 0;
  const uint32_t crc = crc32_ieee(bytes, size);
  header->crc = saved;
  return crc;
}

void sanitize_output_bit(uint8_t& bit, uint8_t fallback) {
  if (bit > 7) bit = fallback;
}

void sanitize_config(Config& config) {
  // Blobs written before the opening cap existed have 0 in that byte (it was
  // tail padding). Any out-of-range value is equally invalid.
  if (config.mix_max_open_pct < 10 || config.mix_max_open_pct > 100) {
    config.mix_max_open_pct = 85;
  }
  if (config.mix_min_open_pct > 100) config.mix_min_open_pct = 40;
  if (config.mix_max_below_pct < 10 || config.mix_max_below_pct > 100) {
    config.mix_max_below_pct = 90;
  }
  if (config.mix_max_above_pct < 10 || config.mix_max_above_pct > 100) {
    config.mix_max_above_pct = 70;
  }
  if (!isfinite(config.mix_max_switch_c) || config.mix_max_switch_c < 0.0f ||
      config.mix_max_switch_c > 90.0f) {
    config.mix_max_switch_c = 50.0f;
  }
  if (!isfinite(config.mix_max_hysteresis_c) || config.mix_max_hysteresis_c < 0.5f ||
      config.mix_max_hysteresis_c > 10.0f) {
    config.mix_max_hysteresis_c = 2.0f;
  }
  if (!isfinite(config.mix_overshoot_delta_c) || config.mix_overshoot_delta_c < 0.5f ||
      config.mix_overshoot_delta_c > 10.0f) {
    config.mix_overshoot_delta_c = 3.0f;
  }
  if (!isfinite(config.mix_overshoot_recovery_c) || config.mix_overshoot_recovery_c < 0.0f ||
      config.mix_overshoot_recovery_c > 10.0f) {
    config.mix_overshoot_recovery_c = 1.0f;
  }
  if (config.mix_overshoot_target_pct > 100) config.mix_overshoot_target_pct = 40;
  if (!isfinite(config.pump_current_w_per_v) || config.pump_current_w_per_v < 1.0f ||
      config.pump_current_w_per_v > 5000.0f) {
    config.pump_current_w_per_v = 588.2f;
  }
  if (!isfinite(config.pump_current_offset_v) || config.pump_current_offset_v < 0.0f ||
      config.pump_current_offset_v > 1.0f) {
    config.pump_current_offset_v = 0.004f;
  }
  if (!isfinite(config.sensor_role_tolerance_c) || config.sensor_role_tolerance_c < 0.5f ||
      config.sensor_role_tolerance_c > 20.0f) {
    config.sensor_role_tolerance_c = 3.0f;
  }
  if (!isfinite(config.dhw_infer_drop_c) || config.dhw_infer_drop_c < 1.0f ||
      config.dhw_infer_drop_c > 20.0f) {
    config.dhw_infer_drop_c = 5.0f;
  }
  if (config.dhw_infer_window_s < 10 || config.dhw_infer_window_s > 3600) {
    config.dhw_infer_window_s = 120;
  }
  if (!isfinite(config.dhw_infer_recovery_c) || config.dhw_infer_recovery_c < 0.0f ||
      config.dhw_infer_recovery_c > 20.0f) {
    config.dhw_infer_recovery_c = 3.0f;
  }
  if (config.dhw_infer_max_s < 60 || config.dhw_infer_max_s > 86400) {
    config.dhw_infer_max_s = 1800;
  }

  // Output-driver block (v3). Reject an unknown driver and keep the expander
  // bit map inside the one-byte range. A standard PCF8574 address is
  // 0x20-0x27 (PCF8574) or 0x38-0x3F (PCF8574A); other 7-bit addresses are
  // allowed for compatible custom expanders.
  if (config.output_expander > OUTPUT_EXPANDER_PCF8574) {
    config.output_expander = OUTPUT_EXPANDER_NONE;
  }
  if (config.expander_address < 0x08 || config.expander_address > 0x77) {
    config.expander_address = 0x20;
  }
  sanitize_output_bit(config.expander_bit_heater, 0);
  sanitize_output_bit(config.expander_bit_pump_rad1, 1);
  sanitize_output_bit(config.expander_bit_pump_rad2, 2);
  sanitize_output_bit(config.expander_bit_pump_floor, 3);
  sanitize_output_bit(config.expander_bit_mix_direction, 4);
  sanitize_output_bit(config.expander_bit_mix_motion, 5);
  sanitize_output_bit(config.expander_bit_mux_s0, 6);
  sanitize_output_bit(config.expander_bit_mux_s1, 7);

  // A minimum above the usable ceiling is contradictory. Clamp it rather than
  // rejecting stored settings; normal API writes validate the combination.
  uint8_t ceiling = config.mix_max_open_pct;
  if (config.mix_max_dynamic) {
    if (config.mix_max_below_pct < ceiling) ceiling = config.mix_max_below_pct;
    if (config.mix_max_above_pct < ceiling) ceiling = config.mix_max_above_pct;
  }
  if (config.mix_min_open_pct > ceiling) config.mix_min_open_pct = ceiling;
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
    // blob. The current format is the largest recorded format, so anything
    // that still fits is read in full and the header decides how to
    // interpret it.
    if (stored < sizeof(ConfigBlobPrefix)) {
      result = ConfigLoadResult::Corrupt;
    } else if (stored > sizeof(ConfigBlob)) {
      // Larger than any recorded format: an old/foreign blob, not damage.
      result = ConfigLoadResult::Stale;
    } else {
      uint8_t bytes[sizeof(ConfigBlob)] = {};
      if (g_prefs.getBytes(kKey, bytes, stored) != stored) {
        result = ConfigLoadResult::Corrupt;
      } else {
        const ConfigBlobHeader* header = reinterpret_cast<const ConfigBlobHeader*>(bytes);
        if (header->magic != kMagic) {
          // Old-format or foreign blob. No migration target: use defaults.
          result = ConfigLoadResult::Stale;
        } else if (header->version == kVersion) {
          if (stored != sizeof(ConfigBlob) || header->size != sizeof(Config)) {
            result = ConfigLoadResult::Corrupt;
          } else if (blob_crc_bytes(bytes, stored) == header->crc) {
            memcpy(&config, bytes + sizeof(ConfigBlobHeader), sizeof(Config));
            sanitize_config(config);
            result = ConfigLoadResult::Loaded;
          } else {
            result = ConfigLoadResult::Corrupt;
          }
        } else if (header->version == 1) {
          const size_t v1_blob_size = sizeof(ConfigBlobHeader) + kV1ConfigSize;
          if (stored != v1_blob_size || header->size != kV1ConfigSize) {
            result = ConfigLoadResult::Corrupt;
          } else if (blob_crc_bytes(bytes, stored) == header->crc) {
            // Start from current defaults so every appended field has a valid
            // value, then overwrite the unchanged v1 prefix.
            Config migrated = config_defaults();
            memcpy(&migrated, bytes + sizeof(ConfigBlobHeader), kV1ConfigSize);
            sanitize_config(migrated);
            config = migrated;
            result = ConfigLoadResult::Migrated;
          } else {
            result = ConfigLoadResult::Corrupt;
          }
        } else if (header->version == 2) {
          const size_t v2_blob_size = sizeof(ConfigBlobHeader) + kV2ConfigSize;
          if (stored != v2_blob_size || header->size != kV2ConfigSize) {
            result = ConfigLoadResult::Corrupt;
          } else if (blob_crc_bytes(bytes, stored) == header->crc) {
            Config migrated = config_defaults();
            memcpy(&migrated, bytes + sizeof(ConfigBlobHeader), kV2ConfigSize);
            sanitize_config(migrated);
            config = migrated;
            result = ConfigLoadResult::Migrated;
          } else {
            result = ConfigLoadResult::Corrupt;
          }
        } else {
          // A newer format written by a different firmware build.
          result = ConfigLoadResult::Stale;
        }
      }
    }
  }

  g_prefs.end();

  switch (result) {
    case ConfigLoadResult::Loaded:
      Serial.println("[config] loaded saved configuration from NVS");
      break;
    case ConfigLoadResult::Migrated:
      Serial.printf("[config] migrated older configuration to v%u\n",
                    static_cast<unsigned>(kVersion));
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
  uint8_t* bytes = reinterpret_cast<uint8_t*>(&blob);
  blob.header.crc = blob_crc_bytes(bytes, sizeof(blob));

  // A second pass verifies that the checksum helper is deterministic before
  // the blob is committed.
  if (blob.header.crc != blob_crc_bytes(bytes, sizeof(blob))) {
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
