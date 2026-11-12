// Host test of the configuration NVS migration (v1-v5, Wi-Fi backup).
//
// Built and run by floor_simulator/tests/test_fw_config_store.py:
//   g++ -std=c++17 -I../../include -Istubs config_store_harness.cpp ../../src/config_store.cpp
//
// config_store.cpp only needs config_defaults() from the rest of the firmware.
// The board defaults are irrelevant here, so this is a compact but valid set.
#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <Preferences.h>

#include "hpn/config.h"
#include "hpn/config_store.h"
#include "hpn/crc32.h"

Config config_defaults() {
  Config c{};
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "shipped-ssid");
  snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", "shipped-pass");
  snprintf(c.ap_ssid, sizeof(c.ap_ssid), "%s", "HeatpumpsN-Setup");
  snprintf(c.ap_password, sizeof(c.ap_password), "%s", "heatpumps-setup");
  snprintf(c.hostname, sizeof(c.hostname), "%s", "heatpumpsn");
  c.setpoint_default = 35.0f;
  c.floor_setpoint_shadow = 35.0f;
  c.t_max_hard = 50.0f;
  c.mix_max_open_pct = 85;
  c.mix_min_open_pct = 40;
  c.mix_max_below_pct = 90;
  c.mix_max_above_pct = 70;
  c.mix_max_switch_c = 50.0f;
  c.mix_max_hysteresis_c = 2.0f;
  c.mix_overshoot_delta_c = 3.0f;
  c.mix_overshoot_recovery_c = 1.0f;
  c.mix_overshoot_target_pct = 40;
  c.pump_current_w_per_v = 588.2f;
  c.pump_current_offset_v = 0.004f;
  c.sensor_role_tolerance_c = 3.0f;
  c.dhw_infer_drop_c = 5.0f;
  c.dhw_infer_window_s = 120;
  c.dhw_infer_recovery_c = 3.0f;
  c.dhw_infer_max_s = 1800;
  c.output_expander = OUTPUT_EXPANDER_74HC595;
  c.expander_address = 0x20;
  c.current_sense = CURRENT_SENSE_MUX_ADC;
  c.ads1115_address = 0x48;
  c.has_pump_power[0] = true;
  c.has_pump_power[1] = true;
  c.has_pump_power[2] = true;
  c.has_mixer_power = false;
  return c;
}

namespace {

constexpr const char* kNs = "hpncfg";
constexpr const char* kKey = "config";
constexpr uint32_t kMagic = 0x48504E43u;  // "HPNC"

struct Header {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
};

void put_blob(uint16_t version, size_t payload_size, const Config& config) {
  std::vector<uint8_t> bytes(sizeof(Header) + payload_size, 0);
  Header header{kMagic, version, static_cast<uint16_t>(payload_size), 0};
  memcpy(bytes.data(), &header, sizeof(header));
  memcpy(bytes.data() + sizeof(header), &config, payload_size);
  Header* stored = reinterpret_cast<Header*>(bytes.data());
  stored->crc = hpn::crc32_ieee(bytes.data(), bytes.size());
  nvs_set_bytes(kNs, kKey, bytes.data(), bytes.size());
}

Config load_with_defaults(hpn::ConfigLoadResult* result = nullptr) {
  Config c = config_defaults();
  const hpn::ConfigLoadResult r = hpn::config_load(c);
  if (result != nullptr) *result = r;
  return c;
}

void expect_str(const char* actual, const char* expected, const char* what) {
  if (strcmp(actual, expected) != 0) {
    std::printf("FAIL %s: expected \"%s\", got \"%s\"\n", what, expected, actual);
    assert(false);
  }
}

void test_round_trip() {
  nvs_map().clear();
  Config c = config_defaults();
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "Korona-IoT");
  snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", "live-pass");
  c.t_max_hard = 48.0f;
  assert(hpn::config_save(c));

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Loaded);
  expect_str(loaded.wifi_ssid, "Korona-IoT", "round-trip SSID");
  expect_str(loaded.wifi_password, "live-pass", "round-trip password");
  assert(loaded.t_max_hard == 48.0f);
  std::printf("ok round-trip\n");
}

void test_v4_migration() {
  nvs_map().clear();
  Config old = config_defaults();
  snprintf(old.wifi_ssid, sizeof(old.wifi_ssid), "%s", "old-v4-ssid");
  snprintf(old.wifi_password, sizeof(old.wifi_password), "%s", "old-v4-pass");
  old.t_max_hard = 47.0f;
  old.has_pump_power[0] = false;
  old.has_pump_power[1] = true;
  old.has_pump_power[2] = false;
  old.has_mixer_power = false;  // v4 tail padding, must be recomputed
  put_blob(4, 1376, old);

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Migrated);
  expect_str(loaded.wifi_ssid, "old-v4-ssid", "v4 SSID");
  expect_str(loaded.wifi_password, "old-v4-pass", "v4 password");
  assert(loaded.t_max_hard == 47.0f);
  assert(loaded.has_mixer_power);  // derived from the pump meters

  // The caller persists the migration; the next boot is a normal load.
  assert(hpn::config_save(loaded));
  Config again = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Loaded);
  expect_str(again.wifi_ssid, "old-v4-ssid", "v4 saved SSID");
  std::printf("ok v4 migration\n");
}

void test_v2_migration() {
  nvs_map().clear();
  Config old = config_defaults();
  snprintf(old.wifi_ssid, sizeof(old.wifi_ssid), "%s", "old-v2-ssid");
  old.t_max_hard = 45.0f;
  old.mixer_meter_scale = 1;
  // The output driver did not exist in v2 and is outside its stored prefix.
  old.output_expander = OUTPUT_EXPANDER_NONE;
  put_blob(2, 1348, old);

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Migrated);
  expect_str(loaded.wifi_ssid, "old-v2-ssid", "v2 SSID");
  assert(loaded.t_max_hard == 45.0f);
  assert(loaded.mixer_meter_scale == 1);
  assert(loaded.output_expander == OUTPUT_EXPANDER_74HC595);  // shipped default
  std::printf("ok v2 migration\n");
}

void test_v1_migration() {
  nvs_map().clear();
  Config old = config_defaults();
  snprintf(old.wifi_ssid, sizeof(old.wifi_ssid), "%s", "old-v1-ssid");
  old.t_max_hard = 44.0f;
  old.has_pump_power[0] = true;
  old.has_pump_power[1] = false;
  old.has_pump_power[2] = false;
  put_blob(1, 1288, old);

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Migrated);
  expect_str(loaded.wifi_ssid, "old-v1-ssid", "v1 SSID");
  assert(loaded.t_max_hard == 44.0f);
  assert(loaded.has_pump_power[0]);
  assert(!loaded.has_pump_power[1]);
  assert(loaded.output_expander == OUTPUT_EXPANDER_74HC595);  // shipped default
  std::printf("ok v1 migration\n");
}

void test_v3_migration() {
  nvs_map().clear();
  Config old = config_defaults();
  snprintf(old.wifi_ssid, sizeof(old.wifi_ssid), "%s", "old-v3-ssid");
  old.t_max_hard = 46.0f;
  old.output_active_low = true;
  old.output_active_low_heater = false;
  old.output_active_low_pump_rad1 = false;
  old.output_active_low_pump_rad2 = false;
  old.output_active_low_pump_floor = false;
  old.output_active_low_mix_direction = false;
  old.output_active_low_mix_motion = false;
  put_blob(3, 1364, old);

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Migrated);
  expect_str(loaded.wifi_ssid, "old-v3-ssid", "v3 SSID");
  assert(loaded.t_max_hard == 46.0f);
  assert(loaded.output_active_low_heater);
  assert(loaded.output_active_low_pump_rad1);
  assert(loaded.output_active_low_pump_rad2);
  assert(loaded.output_active_low_pump_floor);
  assert(loaded.output_active_low_mix_direction);
  assert(loaded.output_active_low_mix_motion);
  std::printf("ok v3 migration\n");
}

void test_corrupt_uses_wifi_backup() {
  nvs_map().clear();
  Config c = config_defaults();
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "Korona-IoT");
  snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", "live-pass");
  c.t_max_hard = 49.0f;
  assert(hpn::config_save(c));

  std::vector<uint8_t>& blob = nvs_map()[NvsKey{kNs, kKey}];
  blob[16] ^= 0xFF;  // damage the payload, keep the header plausible

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Corrupt);
  assert(loaded.t_max_hard == 50.0f);  // shipped default, not the damaged value
  expect_str(loaded.wifi_ssid, "Korona-IoT", "corrupt fallback SSID");
  expect_str(loaded.wifi_password, "live-pass", "corrupt fallback password");
  std::printf("ok corrupt fallback\n");
}

void test_unknown_version_uses_wifi_backup() {
  nvs_map().clear();
  Config c = config_defaults();
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "Korona-IoT");
  snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", "live-pass");
  assert(hpn::config_save(c));

  Config future = config_defaults();
  snprintf(future.wifi_ssid, sizeof(future.wifi_ssid), "%s", "future-ssid");
  put_blob(99, sizeof(Config), future);

  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Stale);
  expect_str(loaded.wifi_ssid, "Korona-IoT", "future-format fallback SSID");
  expect_str(loaded.wifi_password, "live-pass", "future-format fallback password");
  std::printf("ok unknown-version fallback\n");
}

void test_missing_config_uses_wifi_backup() {
  nvs_map().clear();
  Config c = config_defaults();
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "Korona-IoT");
  snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", "live-pass");
  assert(hpn::config_save(c));

  assert(nvs_remove(kNs, kKey));
  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Missing);
  expect_str(loaded.wifi_ssid, "Korona-IoT", "missing-config fallback SSID");
  expect_str(loaded.wifi_password, "live-pass", "missing-config fallback password");
  std::printf("ok missing-config fallback\n");
}

void test_factory_reset_clears_backup() {
  nvs_map().clear();
  Config c = config_defaults();
  snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", "Korona-IoT");
  assert(hpn::config_save(c));
  hpn::config_clear();
  hpn::ConfigLoadResult r;
  Config loaded = load_with_defaults(&r);
  assert(r == hpn::ConfigLoadResult::Missing);
  expect_str(loaded.wifi_ssid, "shipped-ssid", "factory reset SSID");
  std::printf("ok factory reset clears backup\n");
}

}  // namespace

int main() {
  test_round_trip();
  test_v4_migration();
  test_v3_migration();
  test_v2_migration();
  test_v1_migration();
  test_corrupt_uses_wifi_backup();
  test_unknown_version_uses_wifi_backup();
  test_missing_config_uses_wifi_backup();
  test_factory_reset_clears_backup();
  std::printf("all config migration tests passed\n");
  return 0;
}
