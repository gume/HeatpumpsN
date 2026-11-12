#pragma once

#include "hpn/config.h"

// NVS-backed configuration persistence.
//
// The whole Config is stored as one blob protected by a magic value, the exact
// struct size, a format version and a CRC32 checksum. Settings are written only
// on an explicit Save from the portal, not on every tick.
//
// One store for every build. The namespace ("hpncfg"), key ("config"), layout
// and version are identical on every carrier and MCU, so a device can switch
// between the v0.1 and v1.x profiles (or be reflashed) without losing its
// settings. board_apply_defaults() only seeds a device that has no usable blob;
// a successful load always replaces those defaults. Consequently a carrier
// change is not visible in the stored values until the config is cleared.
//
// A firmware update never resets the settings by itself: the NVS blob is read
// on every boot, independently of the image that wrote it. Older formats are
// migrated by copying their unchanged Config prefix onto current defaults, so
// existing settings survive while appended fields receive their default
// values. v1 (1288-byte prefix), v2 (1348 bytes), v3 (1364 bytes), v4 (1376
// bytes) and v5 (1376 bytes) are supported. A blob from an unknown version, or
// one that fails validation, falls back to the compiled defaults; nothing is
// written until the migration/repair is complete.
//
// Because the Config blob changes shape with the format, the Wi-Fi credentials
// (the values that keep the portal reachable) also have a tiny, unversioned
// backup record. It is only consulted when the Config blob cannot be used, so
// an unknown future format or a damaged blob still leaves the station able to
// join its network. The backup is refreshed on every successful config_save().
namespace hpn {

enum class ConfigLoadResult : uint8_t {
  Loaded,    // valid current-format blob restored
  Migrated,  // valid older blob upgraded in memory; caller should save it
  Missing,   // no saved blob; compiled defaults are in effect
  Stale,     // blob from another format version; discarded
  Corrupt,   // blob failed size/CRC validation
};

// Load a saved Config into `config`. Migration starts from current compiled
// defaults and copies the known prefix of the older layout, so fields that did
// not exist in the old format keep their shipped default. On Missing/Stale/
// Corrupt, `config` is left with the defaults (except for Wi-Fi credentials,
// which are restored from the backup record when it is available). Only
// Corrupt indicates damaged data.
ConfigLoadResult config_load(Config& config);

// Persist the whole Config. Returns false if the NVS write failed.
bool config_save(const Config& config);

// Remove the saved blob (factory reset).
void config_clear();

}  // namespace hpn
