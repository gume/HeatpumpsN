#pragma once

// Factory credentials, fixed at build time.
//
// Put your own values in include/hpn/credentials_local.h (git-ignored; copy
// credentials_local.h.example) or pass them as -D flags in platformio.ini.
// Anything not set keeps the defaults below.
//
// The portal passwords never reach the image: the pre-build script
// (src/remote_upload.json) reads HPN_ADMIN_PASSWORD and HPN_MONITOR_PASSWORD
// from the same places and writes only the derived PBKDF2 keys to
// hpn_default_keys.h in the build directory. They are the passwords a fresh
// device, a factory reset and the password recovery build start with; a
// device keeps the passwords set on it across firmware updates.
#if __has_include("hpn/credentials_local.h")
#include "hpn/credentials_local.h"
#endif

#ifndef HPN_WIFI_SSID
#define HPN_WIFI_SSID "Testing"   // case-sensitive
#endif
#ifndef HPN_WIFI_PASSWORD
#define HPN_WIFI_PASSWORD "testing123"
#endif
#ifndef HPN_AP_SSID
#define HPN_AP_SSID "HeatpumpsN-Setup"
#endif
#ifndef HPN_AP_PASSWORD
#define HPN_AP_PASSWORD "heatpumps-setup"   // WPA2: at least 8 characters
#endif

// Generated: HPN_DEFAULT_KEYS, the {salt, k_auth, k_enc} hex strings for
// admin and monitor.
#include "hpn_default_keys.h"
