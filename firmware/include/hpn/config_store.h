#pragma once

#include "hpn/config.h"

// NVS-backed configuration persistence.
//
// The whole Config is stored as one versioned blob. This is deliberately
// simple: settings are written only on an explicit Save from the portal, not
// on every tick. A future per-section schema can replace it without changing
// the call sites.
namespace hpn {

// Load a previously saved Config into `config`. Returns false when no valid
// blob exists; `config` is left untouched in that case.
bool config_load(Config& config);

// Persist the whole Config. Returns false if the NVS write failed.
bool config_save(const Config& config);

// Remove the saved blob (factory reset).
void config_clear();

}  // namespace hpn
