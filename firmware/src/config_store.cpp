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
constexpr uint16_t kVersion = 5;

// One format only, no migration: a blob is accepted when its version and size
// match this build exactly. Any other blob is reported as stale and the
// compiled defaults are used. Bump kVersion whenever the Config layout
// changes (the size check also catches a forgotten bump).
//
// Note: v5 is kept because it is the format the bench device stores today.
constexpr size_t kCurrentConfigSize = 1376;

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
static_assert(sizeof(Config) == kCurrentConfigSize,
              "Config layout changed: bump kVersion and kCurrentConfigSize");

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

void sanitize_optional_output_bit(uint8_t& bit) {
  if (bit != HPN_GPIO_UNASSIGNED && bit > 7) bit = HPN_GPIO_UNASSIGNED;
}

void sanitize_config(Config& config) {
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

  // Output driver. Reject an unknown driver and keep the expander
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
  sanitize_optional_output_bit(config.expander_bit_mux_s0);
  sanitize_optional_output_bit(config.expander_bit_mux_s1);
  sanitize_optional_output_bit(config.expander_bit_mix_fault);

  // Current-measurement front end.
  if (config.current_sense > CURRENT_SENSE_ADS1115) {
    config.current_sense = CURRENT_SENSE_NONE;
  }
  if (config.ads1115_address < 0x08 || config.ads1115_address > 0x77) {
    config.ads1115_address = 0x48;
  }

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
    // blob. Anything that fits is read in full and the header decides.
    if (stored < sizeof(ConfigBlobPrefix)) {
      result = ConfigLoadResult::Corrupt;
    } else if (stored > sizeof(ConfigBlob)) {
      // Not this format: a foreign blob, not damage.
      result = ConfigLoadResult::Stale;
    } else {
      uint8_t bytes[sizeof(ConfigBlob)] = {};
      if (g_prefs.getBytes(kKey, bytes, stored) != stored) {
        result = ConfigLoadResult::Corrupt;
      } else {
        const ConfigBlobHeader* header = reinterpret_cast<const ConfigBlobHeader*>(bytes);
        if (header->magic != kMagic) {
          // Foreign blob: use defaults.
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
        } else {
          // Another format version: no migration, use the compiled defaults.
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
