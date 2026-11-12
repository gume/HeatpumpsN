#include "hpn/config_store.h"

#include <Arduino.h>
#include <Preferences.h>
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "hpn/crc32.h"

namespace hpn {
namespace {

constexpr const char* kNamespace = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"
constexpr uint16_t kVersion = 5;
constexpr size_t kCurrentConfigSize = 1376;

// Each recorded layout is the number of leading bytes of the current Config
// that the old firmware stored. The layouts are append-only: a new field is
// added at the end of Config, the version is bumped, and its layout is added
// here. Migrating copies the old prefix over the current defaults, then the
// version-specific fixups below convert fields whose meaning changed. A layout
// that inserts or removes a field must be handled explicitly instead; do not
// just change a size here.
struct ConfigLayout {
  uint16_t version;
  size_t size;
};

constexpr ConfigLayout kLayouts[] = {
    {1, 1288},  // first CRC32 format
    {2, 1348},  // pump calibration, operating window, sensor roles, inferred DHW
    {3, 1364},  // output driver / port expander, global polarity
    {4, 1376},  // per-channel polarity, fault input, current sensing
    {5, 1376},  // has_mixer_power reuses the v4 tail padding
};
constexpr size_t kLayoutCount = sizeof(kLayouts) / sizeof(kLayouts[0]);

const ConfigLayout* find_layout(uint16_t version) {
  for (size_t i = 0; i < kLayoutCount; ++i) {
    if (kLayouts[i].version == version) return &kLayouts[i];
  }
  return nullptr;
}

struct ConfigBlobHeader {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
};

struct ConfigBlob {
  ConfigBlobHeader header;
  Config config;
};

static_assert(sizeof(ConfigBlobHeader) == 12, "unexpected NVS header layout");
static_assert(sizeof(Config) == kCurrentConfigSize,
              "Config layout changed: bump kVersion, add a layout entry and check the migration");
static_assert(kLayouts[kLayoutCount - 1].version == kVersion,
              "the last recorded layout must be the current Config version");
static_assert(kLayouts[kLayoutCount - 1].size == sizeof(Config),
              "the current layout size must match sizeof(Config)");
// v5 reuses the padding byte v4 left after tlv9064_present. If this fails, a
// field was added somewhere other than the end: bump the version and extend
// the migration path instead of silently reinterpreting stored bytes.
static_assert(offsetof(Config, has_mixer_power) == kLayouts[3].size - 1,
              "v5 must reuse the v4 tail padding byte");

// Wi-Fi credentials backup. Deliberately independent of the Config format:
// this record is only read when the Config blob cannot be used, so a changed
// Config layout or a damaged blob cannot lock the device off its network.
constexpr const char* kWifiNamespace = "hpnwifi";
constexpr const char* kWifiKey = "wifi";
constexpr uint32_t kWifiMagic = 0x48505746u;  // "HPWF"
constexpr uint16_t kWifiVersion = 1;

struct WifiBlob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  char ssid[33];
  char password[64];
};

static_assert(sizeof(WifiBlob) == 112, "unexpected Wi-Fi backup layout");

Preferences g_prefs;

bool open_store() { return g_prefs.begin(kNamespace, false); }

uint32_t wifi_blob_crc(WifiBlob& blob) {
  const uint32_t saved = blob.crc;
  blob.crc = 0;
  const uint32_t crc = crc32_ieee(&blob, sizeof(blob));
  blob.crc = saved;
  return crc;
}

bool wifi_backup_write(const Config& config) {
  WifiBlob blob;
  memset(&blob, 0, sizeof(blob));  // padding is part of the CRC
  blob.magic = kWifiMagic;
  blob.version = kWifiVersion;
  blob.size = sizeof(blob);
  // The arrays have the same size as their Config counterparts; force a
  // terminator in case an imported string filled the whole field.
  memcpy(blob.ssid, config.wifi_ssid, sizeof(blob.ssid));
  memcpy(blob.password, config.wifi_password, sizeof(blob.password));
  blob.ssid[sizeof(blob.ssid) - 1] = '\0';
  blob.password[sizeof(blob.password) - 1] = '\0';
  blob.crc = 0;
  blob.crc = wifi_blob_crc(blob);

  Preferences prefs;
  if (!prefs.begin(kWifiNamespace, false)) return false;
  const bool ok = prefs.putBytes(kWifiKey, &blob, sizeof(blob)) == sizeof(blob);
  prefs.end();
  if (!ok) Serial.println("[config] Wi-Fi backup write failed");
  return ok;
}

bool wifi_backup_read(Config& config) {
  Preferences prefs;
  if (!prefs.begin(kWifiNamespace, true)) return false;
  const size_t stored = prefs.getBytesLength(kWifiKey);
  WifiBlob blob{};
  const bool read = stored == sizeof(blob) &&
                    prefs.getBytes(kWifiKey, &blob, sizeof(blob)) == sizeof(blob);
  prefs.end();
  if (!read || blob.magic != kWifiMagic || blob.version != kWifiVersion ||
      blob.size != sizeof(blob) || blob.crc != wifi_blob_crc(blob)) {
    return false;
  }
  blob.ssid[sizeof(blob.ssid) - 1] = '\0';
  blob.password[sizeof(blob.password) - 1] = '\0';
  snprintf(config.wifi_ssid, sizeof(config.wifi_ssid), "%s", blob.ssid);
  snprintf(config.wifi_password, sizeof(config.wifi_password), "%s", blob.password);
  return true;
}

// Write the backup once when the Config blob is valid but the backup is
// missing or unreadable (a device updated from a firmware that did not have
// this record, or a damaged backup record).
void wifi_backup_ensure(const Config& config) {
  Config probe = config;
  if (!wifi_backup_read(probe)) wifi_backup_write(config);
}

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

float clamp_setpoint(float value_c) {
  if (!isfinite(value_c)) return 35.0f;
  if (value_c < kFloorSetpointMinC) return kFloorSetpointMinC;
  if (value_c > kFloorSetpointMaxC) return kFloorSetpointMaxC;
  return value_c;
}

void sanitize_config(Config& config) {
  config.setpoint_default = clamp_setpoint(config.setpoint_default);
  config.floor_setpoint_shadow = clamp_setpoint(config.floor_setpoint_shadow);
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

// Convert a validated older payload into the current Config. The current
// compiled configuration (with the board profile applied) is the base; the old
// prefix overwrites it and fields that did not exist in the old format keep
// their shipped defaults. Version-specific fixups handle fields whose meaning
// or placement changed without changing the prefix. The result is written in
// place so boot does not need another 1376-byte stack copy.
void migrate_config(uint16_t version, size_t payload_size, const uint8_t* payload,
                    Config& config) {
  config = config_defaults();
  if (payload_size > sizeof(Config)) return;  // guarded by kLayouts
  memcpy(&config, payload, payload_size);

  switch (version) {
    case 3:
      // v3 had one global polarity. Carry it into every output so the relay
      // behaviour does not change during the migration.
      config.output_active_low_heater = config.output_active_low;
      config.output_active_low_pump_rad1 = config.output_active_low;
      config.output_active_low_pump_rad2 = config.output_active_low;
      config.output_active_low_pump_floor = config.output_active_low;
      config.output_active_low_mix_direction = config.output_active_low;
      config.output_active_low_mix_motion = config.output_active_low;
      break;
    case 4:
      // v4 stored the three pump meters only; the v5 byte was tail padding.
      // Every v4 carrier wired the same four rings, so a board with any pump
      // meter also had the mixer meter; a boardless build had none.
      config.has_mixer_power = config.has_pump_power[0] || config.has_pump_power[1] ||
                               config.has_pump_power[2];
      break;
    default:
      // v1/v2 are pure prefixes; v5 is the current layout.
      break;
  }

  sanitize_config(config);
}

}  // namespace

ConfigLoadResult config_load(Config& config) {
  if (!open_store()) {
    Serial.println("[config] NVS open failed; using compiled defaults");
    if (wifi_backup_read(config)) {
      Serial.println("[config] Wi-Fi credentials restored from the backup record");
    }
    return ConfigLoadResult::Corrupt;
  }

  const size_t stored = g_prefs.getBytesLength(kKey);
  ConfigLoadResult result = ConfigLoadResult::Missing;
  uint16_t migrated_from = 0;

  if (stored != 0) {
    // Preferences::getBytes() requires a buffer large enough for the whole
    // blob. Anything that fits is read in full and the header decides.
    if (stored < sizeof(ConfigBlobHeader)) {
      result = ConfigLoadResult::Corrupt;
    } else if (stored > sizeof(ConfigBlob)) {
      // Not a format this build knows: a foreign or newer blob, not damage.
      result = ConfigLoadResult::Stale;
    } else {
      uint8_t bytes[sizeof(ConfigBlob)] = {};
      if (g_prefs.getBytes(kKey, bytes, stored) != stored) {
        result = ConfigLoadResult::Corrupt;
      } else {
        const ConfigBlobHeader* header = reinterpret_cast<const ConfigBlobHeader*>(bytes);
        const ConfigLayout* layout = find_layout(header->version);
        if (header->magic != kMagic || layout == nullptr) {
          // Foreign or unknown version. Keep the defaults rather than
          // reinterpreting bytes we do not understand.
          result = ConfigLoadResult::Stale;
        } else if (stored != sizeof(ConfigBlobHeader) + layout->size ||
                   header->size != layout->size) {
          result = ConfigLoadResult::Corrupt;
        } else if (blob_crc_bytes(bytes, stored) != header->crc) {
          result = ConfigLoadResult::Corrupt;
        } else if (header->version == kVersion) {
          memcpy(&config, bytes + sizeof(ConfigBlobHeader), sizeof(Config));
          sanitize_config(config);
          result = ConfigLoadResult::Loaded;
        } else {
          migrate_config(header->version, layout->size,
                         bytes + sizeof(ConfigBlobHeader), config);
          migrated_from = header->version;
          result = ConfigLoadResult::Migrated;
        }
      }
    }
  }

  g_prefs.end();

  switch (result) {
    case ConfigLoadResult::Loaded:
      Serial.println("[config] loaded saved configuration from NVS");
      wifi_backup_ensure(config);
      break;
    case ConfigLoadResult::Migrated:
      Serial.printf("[config] migrated saved configuration from v%u to v%u\n",
                    static_cast<unsigned>(migrated_from), static_cast<unsigned>(kVersion));
      break;
    case ConfigLoadResult::Stale:
      Serial.printf("[config] saved configuration format differs (%u bytes); using compiled defaults\n",
                    static_cast<unsigned>(stored));
      if (wifi_backup_read(config)) {
        Serial.println("[config] Wi-Fi credentials restored from the backup record");
      }
      break;
    case ConfigLoadResult::Corrupt:
      Serial.printf("[config] NVS CRC/format check failed (%u bytes); using compiled defaults\n",
                    static_cast<unsigned>(stored));
      if (wifi_backup_read(config)) {
        Serial.println("[config] Wi-Fi credentials restored from the backup record");
      }
      break;
    case ConfigLoadResult::Missing:
    default:
      Serial.println("[config] no saved configuration; using compiled defaults");
      if (wifi_backup_read(config)) {
        Serial.println("[config] Wi-Fi credentials restored from the backup record");
      }
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
  if (ok) wifi_backup_write(config);
  return ok;
}

void config_clear() {
  if (!open_store()) {
    return;
  }
  g_prefs.remove(kKey);
  g_prefs.end();

  Preferences wifi;
  if (wifi.begin(kWifiNamespace, false)) {
    wifi.remove(kWifiKey);
    wifi.end();
  }
  Serial.println("[config] saved configuration cleared");
}

}  // namespace hpn
