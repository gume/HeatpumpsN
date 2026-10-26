#pragma once

#include "hpn/config.h"

// NVS-backed configuration persistence.
//
// The whole Config is stored as one blob protected by a magic value, the exact
// struct size, a format version and a CRC32 checksum. Settings are written only
// on an explicit Save from the portal, not on every tick.
//
// There is deliberately no migration path: a blob from another firmware build
// is rejected as corrupt and compiled defaults are used. This keeps the store
// simple while the hardware is still being tested on separate boards.
namespace hpn {

enum class ConfigLoadResult : uint8_t {
  Loaded,   // valid blob restored
  Missing,  // no saved blob; compiled defaults are in effect
  Stale,    // blob from another format version; discarded without migration
  Corrupt,  // current-format blob failed size/CRC validation
};

// Load a saved Config into `config`. On Missing/Stale/Corrupt, `config` is left
// untouched (the caller has usually seeded it with compiled defaults). Only
// Corrupt indicates damaged data; Stale is an old-format blob that is silently
// discarded because this firmware has no migration path.
ConfigLoadResult config_load(Config& config);

// Persist the whole Config. Returns false if the NVS write failed.
bool config_save(const Config& config);

// Remove the saved blob (factory reset).
void config_clear();

}  // namespace hpn
