#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

// Partition layout migration and OTA image validation (docs/partition-migration.md).
//
// Every build uses one table (partitions.csv) whose otadata and app slots sit
// where ESPHome's 4 MB layout has them. A device still on another table (an
// ESPHome board after its first OTA, or a bench device on the previous layout)
// runs this firmware from its current slot and is migrated in place on an
// explicit admin request: the table sector is rewritten, the data partitions
// are re-created, and the firmware's own settings are carried across in a
// stash sector.
//
// After an OTA the bootloader starts the new image in PENDING_VERIFY. It is
// marked valid only after 2 minutes with Wi-Fi up or an admin login; if
// neither happens within 10 minutes the previous image is restored.
namespace hpn {
namespace layout {

// Call in setup() before the configuration is loaded: restores settings that
// a migration stashed (also when it stopped before the table write) and
// clears the stash.
void restore_stash();

// Call about once a second (any task): OTA image validation.
void tick(bool wifi_connected);
void note_admin_login();

// Layout, running slot and image state; `with_tables` adds both tables as hex.
void status_json(JsonObject out, bool with_tables);

// Migrates to the built-in table. Returns false with a reason if a
// precondition fails; on success the device reboots and this never returns.
bool migrate(char* err, size_t err_size);

}  // namespace layout
}  // namespace hpn
