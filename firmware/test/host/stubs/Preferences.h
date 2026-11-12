// Minimal in-memory stand-in for the Arduino Preferences (NVS) API.
//
// The host tests inspect and seed the same map; keys are namespace/key pairs.
// Only the subset used by the storage code is implemented.
#pragma once

#include <stdint.h>
#include <string.h>

#include <map>
#include <string>
#include <vector>

struct NvsKey {
  std::string ns;
  std::string key;
  bool operator<(const NvsKey& other) const {
    return ns != other.ns ? ns < other.ns : key < other.key;
  }
};

inline std::map<NvsKey, std::vector<uint8_t>>& nvs_map() {
  static std::map<NvsKey, std::vector<uint8_t>> store;
  return store;
}

inline bool nvs_set_bytes(const char* ns, const char* key, const void* data, size_t len) {
  std::vector<uint8_t>& value = nvs_map()[NvsKey{ns, key}];
  value.assign(static_cast<const uint8_t*>(data), static_cast<const uint8_t*>(data) + len);
  return true;
}

inline bool nvs_remove(const char* ns, const char* key) {
  return nvs_map().erase(NvsKey{ns, key}) != 0;
}

inline bool nvs_has(const char* ns, const char* key) {
  return nvs_map().count(NvsKey{ns, key}) != 0;
}

inline size_t nvs_len(const char* ns, const char* key) {
  auto it = nvs_map().find(NvsKey{ns, key});
  return it == nvs_map().end() ? 0 : it->second.size();
}

class Preferences {
 public:
  bool begin(const char* ns, bool read_only = false) {
    ns_ = ns;
    read_only_ = read_only;
    return true;
  }
  void end() { ns_.clear(); }
  size_t getBytesLength(const char* key) const { return nvs_len(ns_.c_str(), key); }
  size_t getBytes(const char* key, void* out, size_t len) const {
    auto it = nvs_map().find(NvsKey{ns_, key});
    if (it == nvs_map().end() || it->second.size() != len) return 0;
    memcpy(out, it->second.data(), len);
    return len;
  }
  size_t putBytes(const char* key, const void* data, size_t len) {
    if (read_only_) return 0;
    nvs_set_bytes(ns_.c_str(), key, data, len);
    return len;
  }
  bool remove(const char* key) { return nvs_remove(ns_.c_str(), key); }
  bool isKey(const char* key) const { return nvs_has(ns_.c_str(), key); }

 private:
  std::string ns_;
  bool read_only_ = false;
};
