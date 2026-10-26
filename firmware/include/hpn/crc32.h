#pragma once

#include <stddef.h>
#include <stdint.h>

// Small portable CRC32 (IEEE 802.3, reflected, polynomial 0xEDB88320).
//
// The NVS blobs carry this checksum so a bit flip or a torn write is detected
// instead of being accepted because its length and magic still look plausible.
namespace hpn {

inline uint32_t crc32_ieee(const void* data, size_t length) {
  const uint8_t* bytes = static_cast<const uint8_t*>(data);
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < length; ++i) {
    crc ^= bytes[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      const uint32_t mask = 0u - (crc & 1u);
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

}  // namespace hpn
