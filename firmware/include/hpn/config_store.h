#pragma once

#include "hpn/config.h"

// NVS-backed configuration persistence.
//
// The whole Config is stored as one blob protected by a magic value, the exact
// struct size, a format version and a CRC32 checksum. Settings are written only
// on an explicit Save from the portal, not on every tick.
//
// Older formats are migrated by copying their unchanged Config prefix onto
// current defaults, so existing settings survive while appended fields receive
// their default values. v1 (1288-byte prefix), v2 (1348 bytes), v3 (1364
// bytes) and v4 (1376 bytes) are supported. Any other version is treated as
// stale and discarded.
namespace hpn {

enum class ConfigLoadResult : uint8_t {
  Loaded,    // valid current-format blob restored
  Migrated,  // valid older blob upgraded in memory; caller should save it
  Missing,   // no saved blob; compiled defaults are in effect
  Stale,     // blob from another format version; discarded without migration
  Corrupt,   // current-format blob failed size/CRC validation
};

// Load a saved Config into `config`. On Missing/Stale/Corrupt, `config` is left
// untouched (the caller has usually seeded it with compiled defaults). Only
// Corrupt indicates damaged data. Migrated means the caller should persist the
// upgraded configuration with config_save().
ConfigLoadResult config_load(Config& config);

// Persist the whole Config. Returns false if the NVS write failed.
bool config_save(const Config& config);

// Remove the saved blob (factory reset).
void config_clear();

}  // namespace hpn
