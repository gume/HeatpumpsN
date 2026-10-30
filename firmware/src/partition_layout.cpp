#include "hpn/partition_layout.h"

#include <Arduino.h>
#include <Preferences.h>
#include <esp_flash.h>
#include <esp_flash_partitions.h>
#include <esp_heap_caps.h>
#include <esp_image_format.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_rom_crc.h>
#include <mbedtls/md5.h>
#include <string.h>

#include "hpn/crc32.h"
#include "hpn/tasks.h"
#include "hpn/web_portal.h"

// The Arduino core marks a new OTA image valid during start-up unless this
// returns true; this firmware validates it later (tick()).
extern "C" bool verifyRollbackLater() { return true; }

namespace hpn {
namespace layout {
namespace {

// Keep in sync with partitions.csv (checked against the build's
// partitions.bin before a migration: see docs/partition-migration.md).
struct Entry {
  const char* label;
  uint8_t type;
  uint8_t subtype;
  uint32_t offset;
  uint32_t size;
};
constexpr Entry kTarget[] = {
    {"otadata", 0x01, 0x00, 0x9000, 0x2000},       {"nvs", 0x01, 0x02, 0xB000, 0x5000},
    {"app0", 0x00, 0x10, 0x10000, 0x1C0000},       {"app1", 0x00, 0x11, 0x1D0000, 0x1C0000},
    {"spiffs", 0x01, 0x82, 0x390000, 0x60000},     {"coredump", 0x01, 0x03, 0x3F0000, 0x10000},
};
constexpr size_t kEntries = sizeof(kTarget) / sizeof(kTarget[0]);
constexpr uint32_t kTableOffset = 0x8000;
constexpr uint32_t kTableLen = ESP_PARTITION_TABLE_MAX_LEN;   // 0xC00
constexpr uint32_t kOtadata = 0x9000;
constexpr uint32_t kNvsStart = 0xB000, kNvsEnd = 0x10000;
constexpr uint32_t kDataStart = 0x390000;
constexpr uint32_t kStash = 0x3FF000;                          // last sector (coredump area)
constexpr uint32_t kSector = 0x1000;
constexpr uint32_t kStashMagic = 0x484E5354u;                  // "HNST"
constexpr uint32_t kValidAfterMs = 120000;
constexpr uint32_t kRollbackAfterMs = 600000;

// Table and stash buffers come from internal RAM on the heap: the ESP32-S2
// has no static DRAM to spare, the web task's stack is small, and the ROM
// writer cannot reach PSRAM while the caches are off.
struct Buf {
  explicit Buf(size_t n) : p(static_cast<uint8_t*>(heap_caps_malloc(n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT))) {}
  ~Buf() { heap_caps_free(p); }
  Buf(const Buf&) = delete;
  Buf& operator=(const Buf&) = delete;
  uint8_t* p;
};

// Progress of the last migration, kept across a reset (watchdog, panic) so the
// partitions status can say where it stopped. Not touched by a power loss.
struct Trace {
  uint32_t magic;
  uint32_t step;      // 1 stash, 2 otadata, 3 nvs, 4 data, 5 table, 6 restore old table, 7 restart
  uint32_t address;   // last flash address worked on
  uint32_t ms;        // millis() since the start
  uint32_t start_ms;
};
constexpr uint32_t kTraceMagic = 0x48545243u;
RTC_NOINIT_ATTR Trace g_trace;

void trace(uint32_t step, uint32_t address = 0) {
  g_trace.step = step;
  g_trace.address = address;
  g_trace.ms = millis() - g_trace.start_ms;
}

bool g_admin_seen = false;
bool g_resolved = false;
bool g_restored = false;

void build_table(uint8_t* out) {
  memset(out, 0xFF, kTableLen);
  for (size_t i = 0; i < kEntries; ++i) {
    esp_partition_info_t e{};
    e.magic = ESP_PARTITION_MAGIC;
    e.type = kTarget[i].type;
    e.subtype = kTarget[i].subtype;
    e.pos.offset = kTarget[i].offset;
    e.pos.size = kTarget[i].size;
    strncpy(reinterpret_cast<char*>(e.label), kTarget[i].label, sizeof(e.label));
    e.flags = 0;
    memcpy(out + i * sizeof(e), &e, sizeof(e));
  }
  // MD5 entry, as gen_esp32part.py writes it: EB EB, 14 x FF, MD5 of the entries.
  uint8_t* md5 = out + kEntries * sizeof(esp_partition_info_t);
  md5[0] = 0xEB;
  md5[1] = 0xEB;
  mbedtls_md5(out, kEntries * sizeof(esp_partition_info_t), md5 + 16);
}

bool read_table(uint8_t* out) { return esp_flash_read(nullptr, out, kTableOffset, kTableLen) == ESP_OK; }

// Number of bytes up to and including the MD5 entry (or the last entry).
size_t used_len(const uint8_t* t) {
  size_t n = 0;
  for (size_t off = 0; off + 32 <= kTableLen; off += 32) {
    const uint16_t magic = static_cast<uint16_t>(t[off] | (t[off + 1] << 8));
    if (magic != ESP_PARTITION_MAGIC && magic != ESP_PARTITION_MAGIC_MD5) break;
    n = off + 32;
  }
  return n;
}

bool find_app(const uint8_t* t, uint8_t subtype, uint32_t* offset) {
  for (size_t off = 0; off + 32 <= kTableLen; off += 32) {
    const esp_partition_info_t* e = reinterpret_cast<const esp_partition_info_t*>(t + off);
    if (e->magic != ESP_PARTITION_MAGIC) break;
    if (e->type == 0x00 && e->subtype == subtype) {
      *offset = e->pos.offset;
      return true;
    }
  }
  return false;
}

const char* classify(const uint8_t* current, const uint8_t* target) {
  if (memcmp(current, target, kTableLen) == 0) return "current";
  uint32_t a0 = 0, a1 = 0;
  find_app(current, 0x10, &a0);
  find_app(current, 0x11, &a1);
  if (a0 == 0x10000 && a1 == 0x1D0000) return "esphome";
  if (a0 == 0x10000 && a1 == 0x1A0000) return "previous";
  return "unknown";
}

const char* state_name(esp_ota_img_states_t s) {
  switch (s) {
    case ESP_OTA_IMG_NEW: return "new";
    case ESP_OTA_IMG_PENDING_VERIFY: return "pending_verify";
    case ESP_OTA_IMG_VALID: return "valid";
    case ESP_OTA_IMG_INVALID: return "invalid";
    case ESP_OTA_IMG_ABORTED: return "aborted";
    default: return "undefined";
  }
}

esp_ota_img_states_t running_state() {
  esp_ota_img_states_t s = ESP_OTA_IMG_UNDEFINED;
  esp_ota_get_state_partition(esp_ota_get_running_partition(), &s);
  return s;
}

uint32_t running_image_len() {
  const esp_partition_t* run = esp_ota_get_running_partition();
  const esp_partition_pos_t pos = {run->address, run->size};
  esp_image_metadata_t meta{};
  return esp_image_verify(ESP_IMAGE_VERIFY_SILENT, &pos, &meta) == ESP_OK ? meta.image_len : 0;
}

// Why a migration cannot run now, or nullptr.
const char* blocker(const uint8_t* current, const uint8_t* target) {
  if (memcmp(current, target, kTableLen) == 0) return "already on the current layout";
  const esp_partition_t* run = esp_ota_get_running_partition();
  if (run->address != 0x10000 && run->address != 0x1D0000) {
    return "running from a slot the new layout does not have: upload this firmware once more "
           "(it lands in app0), then migrate";
  }
  const uint32_t len = running_image_len();
  if (len == 0 || len > 0x1C0000) return "running image does not fit a 1.75 MB slot";
  // NEW stays NEW under a bootloader without rollback support (it never
  // moves the image to PENDING_VERIFY), so it counts as confirmed.
  const esp_ota_img_states_t s = running_state();
  if (s != ESP_OTA_IMG_VALID && s != ESP_OTA_IMG_UNDEFINED && s != ESP_OTA_IMG_NEW) {
    return "this image is not confirmed yet (wait up to 2 minutes after the update)";
  }
  return nullptr;
}

// --- settings stash ----------------------------------------------------------
struct StashItem {
  const char* ns;
  const char* key;
  bool u64;
};
constexpr StashItem kStashItems[] = {
    {"hpncfg", "config", false}, {"hpnsec", "blob", false}, {"hpnsec", "seq", true}, {"hpnmpc", "blob", false},
};

// Layout: magic, total length, CRC32 of the payload; payload = records of
// ns[16] key[16] u8 is_u64 u8 pad[3] u32 len, data (padded to 4).
bool write_stash() {
  Buf mem(kSector);
  Buf item(1600);
  if (!mem.p || !item.p) return false;
  uint8_t* buf = mem.p;
  memset(buf, 0xFF, kSector);
  size_t off = 12;
  for (const StashItem& it : kStashItems) {
    Preferences p;
    if (!p.begin(it.ns, true)) continue;
    size_t len = 0;
    uint8_t* data = item.p;
    if (it.u64) {
      if (!p.isKey(it.key)) { p.end(); continue; }
      const uint64_t v = p.getULong64(it.key, 0);
      memcpy(data, &v, 8);
      len = 8;
    } else {
      len = p.getBytesLength(it.key);
      if (len == 0 || len > 1600 || p.getBytes(it.key, data, len) != len) { p.end(); continue; }
    }
    p.end();
    const size_t rec = 40 + ((len + 3) & ~size_t(3));
    if (off + rec > kSector) return false;
    memset(buf + off, 0, 40);
    strncpy(reinterpret_cast<char*>(buf + off), it.ns, 16);
    strncpy(reinterpret_cast<char*>(buf + off + 16), it.key, 16);
    buf[off + 32] = it.u64 ? 1 : 0;
    const uint32_t l32 = len;
    memcpy(buf + off + 36, &l32, 4);
    memcpy(buf + off + 40, data, len);
    off += rec;
  }
  const uint32_t magic = kStashMagic, total = off;
  const uint32_t crc = crc32_ieee(buf + 12, off - 12);
  memcpy(buf, &magic, 4);
  memcpy(buf + 4, &total, 4);
  memcpy(buf + 8, &crc, 4);
  if (esp_flash_erase_region(nullptr, kStash, kSector) != ESP_OK) return false;
  return esp_flash_write(nullptr, buf, kStash, (off + 3) & ~size_t(3)) == ESP_OK;
}

// The one write the flash driver refuses (CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS):
// the partition table sector. The refusal is the chip's region_protected OS
// hook, so the write goes through the normal driver (locking, caches, flash
// mode) with that hook cleared for its duration. The ROM routines are no
// option: under IDF 5 they hang polling a SPI1 controller the driver has
// reconfigured (seen on the bench). Static: another task may still hold the
// pointer for a moment after it is put back.
esp_flash_os_functions_t g_open_os_func;

bool write_table_sector(const uint8_t* buf) {
  esp_flash_t* chip = esp_flash_default_chip;
  const esp_flash_os_functions_t* saved = chip->os_func;
  g_open_os_func = *saved;
  g_open_os_func.region_protected = nullptr;
  chip->os_func = &g_open_os_func;
  const bool ok = esp_flash_erase_region(chip, kTableOffset, kSector) == ESP_OK &&
                  esp_flash_write(chip, buf, kTableOffset, kTableLen) == ESP_OK;
  chip->os_func = saved;
  return ok;
}

// One 4 KB sector at a time with a pause in between: on an aligned range the
// driver uses 64 KB block erases, which stall flash access on both cores for
// up to a second and starve T_io past its watchdog deadline (200 ms).
bool erase_paced(uint32_t start, uint32_t end) {
  for (uint32_t a = start; a < end; a += kSector) {
    g_trace.address = a;
    g_trace.ms = millis() - g_trace.start_ms;
    if (esp_flash_erase_region(nullptr, a, kSector) != ESP_OK) return false;
    vTaskDelay(pdMS_TO_TICKS(5));
  }
  return true;
}

bool write_otadata(uint32_t slot) {
  esp_ota_select_entry_t e;
  memset(&e, 0xFF, sizeof(e));
  e.ota_seq = slot + 1;   // the bootloader boots slot (seq - 1) % 2
  e.ota_state = ESP_OTA_IMG_VALID;
  e.crc = esp_rom_crc32_le(UINT32_MAX, reinterpret_cast<const uint8_t*>(&e.ota_seq), 4);
  return erase_paced(kOtadata, kOtadata + 2 * kSector) &&
         esp_flash_write(nullptr, &e, kOtadata, sizeof(e)) == ESP_OK;
}

void to_hex(const uint8_t* in, size_t n, String& out) {
  static const char* digits = "0123456789abcdef";
  out.reserve(n * 2);
  for (size_t i = 0; i < n; ++i) {
    out += digits[in[i] >> 4];
    out += digits[in[i] & 15];
  }
}

}  // namespace

// Runs on any layout: after a migration the stash fills the new NVS, and
// after one that stopped before the table write (power loss, reset) it puts
// back the settings that the erase of the old NVS removed.
void restore_stash() {
  Buf mem(kSector);
  if (!mem.p) return;
  uint8_t* buf = mem.p;
  if (esp_flash_read(nullptr, buf, kStash, kSector) != ESP_OK) return;
  uint32_t magic, total, crc;
  memcpy(&magic, buf, 4);
  memcpy(&total, buf + 4, 4);
  memcpy(&crc, buf + 8, 4);
  if (magic != kStashMagic || total < 12 || total > kSector || crc32_ieee(buf + 12, total - 12) != crc) return;
  size_t off = 12;
  while (off + 40 <= total) {
    char ns[17] = {}, key[17] = {};
    memcpy(ns, buf + off, 16);
    memcpy(key, buf + off + 16, 16);
    const bool u64 = buf[off + 32] != 0;
    uint32_t len;
    memcpy(&len, buf + off + 36, 4);
    Preferences p;
    if (p.begin(ns, false)) {
      if (u64) {
        uint64_t v;
        memcpy(&v, buf + off + 40, 8);
        p.putULong64(key, v);
      } else {
        p.putBytes(key, buf + off + 40, len);
      }
      p.end();
    }
    off += 40 + ((len + 3) & ~3u);
  }
  esp_flash_erase_region(nullptr, kStash, kSector);
  g_restored = true;
  Serial.println("[layout] settings restored from the migration stash");
}

void note_admin_login() { g_admin_seen = true; }

void tick(bool wifi_connected) {
  if (g_resolved) return;
  if (running_state() != ESP_OTA_IMG_PENDING_VERIFY) {
    g_resolved = true;
    return;
  }
  const uint32_t up = millis();
  if (up >= kValidAfterMs && (wifi_connected || g_admin_seen)) {
    esp_ota_mark_app_valid_cancel_rollback();
    web::log_event(0, 1720, "Firmware image confirmed (rollback cancelled)");
    g_resolved = true;
  } else if (up >= kRollbackAfterMs) {
    web::log_event(2, 1721, "Firmware image not confirmed in 10 min: rolling back");
    delay(500);
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
}

void status_json(JsonObject out, bool with_tables) {
  Buf cur(kTableLen), tgt(kTableLen);
  if (!cur.p || !tgt.p) {
    out["layout"] = "out of memory";
    return;
  }
  uint8_t* current = cur.p;
  uint8_t* target = tgt.p;
  build_table(target);
  const bool read = read_table(current);
  out["layout"] = read ? classify(current, target) : "unreadable";
  const char* why = read ? blocker(current, target) : "partition table unreadable";
  out["migrationReady"] = why == nullptr;
  out["migrationBlocker"] = why ? why : "";
  out["restoredAfterMigration"] = g_restored;
  if (g_trace.magic == kTraceMagic && g_trace.step <= 7) {
    JsonObject t = out["lastMigration"].to<JsonObject>();
    t["step"] = g_trace.step;
    char at[12];
    snprintf(at, sizeof(at), "0x%06x", static_cast<unsigned>(g_trace.address));
    t["address"] = at;
    t["ms"] = g_trace.ms;
  }
  const esp_partition_t* run = esp_ota_get_running_partition();
  JsonObject r = out["running"].to<JsonObject>();
  r["label"] = run->label;
  char hex[12];
  snprintf(hex, sizeof(hex), "0x%06x", static_cast<unsigned>(run->address));
  r["address"] = hex;
  r["imageLen"] = running_image_len();
  const esp_ota_img_states_t s = running_state();
  r["state"] = state_name(s);
  if (s == ESP_OTA_IMG_PENDING_VERIFY) {
    r["confirmInS"] = millis() < kValidAfterMs ? (kValidAfterMs - millis()) / 1000 : 0;
    r["rollbackInS"] = millis() < kRollbackAfterMs ? (kRollbackAfterMs - millis()) / 1000 : 0;
  }
  if (with_tables) {
    String t, c;
    to_hex(target, used_len(target), t);
    if (read) to_hex(current, used_len(current), c);
    out["targetTable"] = t;
    out["currentTable"] = c;
  }
}

bool migrate(char* err, size_t err_size) {
  Buf cur(kTableLen), tgt(kTableLen), chk(kTableLen);
  if (!cur.p || !tgt.p || !chk.p) {
    snprintf(err, err_size, "out of memory");
    return false;
  }
  uint8_t* current = cur.p;
  uint8_t* target = tgt.p;
  uint8_t* check = chk.p;
  build_table(target);
  if (!read_table(current)) {
    snprintf(err, err_size, "partition table unreadable");
    return false;
  }
  const char* why = blocker(current, target);
  if (why != nullptr) {
    snprintf(err, err_size, "%s", why);
    return false;
  }
  const uint32_t slot = esp_ota_get_running_partition()->address == 0x10000 ? 0 : 1;
  web::log_event(1, 1710, "Partition migration started");
  tasks::maintenance_grace(30000);
  Serial.println("[layout] migration: stash, otadata, data areas, table");

  // Order chosen so that a power loss at any step still boots this image:
  // until the table is written the old table applies, and its otadata (or the
  // ota_0 default) selects the running slot.
  g_trace.magic = kTraceMagic;
  g_trace.start_ms = millis();
  trace(1, kStash);
  if (!write_stash()) {
    snprintf(err, err_size, "could not stash the settings");
    return false;
  }
  trace(2, kOtadata);
  bool erased = write_otadata(slot);
  trace(3, kNvsStart);
  erased = erased && erase_paced(kNvsStart, kNvsEnd);
  trace(4, kDataStart);
  erased = erased && erase_paced(kDataStart, kStash);
  if (!erased) {
    snprintf(err, err_size, "erasing the data areas failed");
    web::log_event(2, 1711, err);
    return false;
  }
  bool ok = false;
  trace(5, kTableOffset);
  for (int attempt = 0; attempt < 2 && !ok; ++attempt) {
    ok = write_table_sector(target) && read_table(check) && memcmp(check, target, kTableLen) == 0;
  }
  if (!ok) {
    // Put the old table back: the device keeps running on its old layout.
    trace(6, kTableOffset);
    const bool restored = write_table_sector(current) && read_table(check) && memcmp(check, current, kTableLen) == 0;
    snprintf(err, err_size, restored ? "table write did not verify; old table restored"
                                     : "table write did not verify and the old table could not be restored");
    web::log_event(3, 1712, err);
    return false;
  }
  trace(7);
  web::log_event(1, 1713, "Partition table migrated; rebooting");
  Serial.println("[layout] table written and verified; rebooting");
  delay(1000);
  esp_restart();
  return true;
}

}  // namespace layout
}  // namespace hpn
