#pragma once

// Compile-time target description.
//
// Exactly one HPN_TARGET_* macro is supplied by platformio.ini. This header
// describes MCU capabilities only: the target name and whether a second core
// exists to pin real-time tasks to. Features are never gated here — the carrier
// profile in board.cpp and the runtime Config decide what the controller does,
// so every MCU build exposes the same feature set.
//
// ESP32-S2 and ESP32-C3 are single-core parts and must not use
// xTaskCreatePinnedToCore.

#if defined(HPN_TARGET_ESP32DEV)
#define HPN_TARGET_NAME "esp32dev"
#define HPN_TARGET_FAMILY "ESP32"
#define HPN_HAS_DUAL_CORE 1
#elif defined(HPN_TARGET_ESP32S3)
#define HPN_TARGET_NAME "esp32s3"
#define HPN_TARGET_FAMILY "ESP32-S3"
#define HPN_HAS_DUAL_CORE 1
#elif defined(HPN_TARGET_ESP32S2)
#define HPN_TARGET_NAME "esp32s2"
#define HPN_TARGET_FAMILY "ESP32-S2"
#define HPN_HAS_DUAL_CORE 0
#elif defined(HPN_TARGET_ESP32C3)
#define HPN_TARGET_NAME "esp32c3"
#define HPN_TARGET_FAMILY "ESP32-C3"
#define HPN_HAS_DUAL_CORE 0
#else
#error "No HPN_TARGET_* macro defined. Build through a PlatformIO env in platformio.ini."
#endif

#define HPN_HAS_WIFI 1
