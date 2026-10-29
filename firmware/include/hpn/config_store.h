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
// There is no migration: a blob from another format version is discarded and
// the compiled defaults are used (Stale).
namespace hpn {

enum class ConfigLoadResult : uint8_t {
  Loaded,    // valid current-format blob restored
  Missing,   // no saved blob; compiled defaults are in effect
  Stale,     // blob from another format version; discarded
  Corrupt,   // current-format blob failed size/CRC validation
};

// Load a saved Config into `config`. On Missing/Stale/Corrupt, `config` is left
// untouched (the caller has usually seeded it with compiled defaults). Only
// Corrupt indicates damaged data.
ConfigLoadResult config_load(Config& config);

// Persist the whole Config. Returns false if the NVS write failed.
bool config_save(const Config& config);

// Remove the saved blob (factory reset).
void config_clear();

}  // namespace hpn
