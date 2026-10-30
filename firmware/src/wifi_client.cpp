#include "hpn/wifi_client.h"

#include <Arduino.h>
#include <WiFi.h>
#include <string.h>
#include <time.h>

#include "hpn/logger.h"

namespace hpn {
namespace wifi {
namespace {

constexpr uint32_t kRetryIntervalMs = 10000;
constexpr uint32_t kRssiLogIntervalMs = 60000;
constexpr uint32_t kApAfterBootMs = 120000;      // never connected since boot
constexpr uint32_t kApAfterLossMs = 300000;      // link down this long
constexpr uint32_t kApIdleCloseMs = 60000;       // station up and AP unused this long
constexpr uint32_t kRetryWithApClientMs = 120000;  // a station retry hops channels
constexpr const char* kApFallbackPassword = "heatpumps-setup";

Status g_status{};
uint32_t g_last_attempt_ms = 0;
uint32_t g_last_rssi_log_ms = 0;
wl_status_t g_last_wl_status = WL_IDLE_STATUS;
bool g_started = false;
bool g_scanned = false;
bool g_ntp_started = false;
uint32_t g_down_since_ms = 0;   // 0 while connected
uint32_t g_ap_idle_since_ms = 0;

const char* wl_name(wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS:
      return "idle";
    case WL_NO_SSID_AVAIL:
      return "no-ssid";
    case WL_SCAN_COMPLETED:
      return "scan-completed";
    case WL_CONNECTED:
      return "connected";
    case WL_CONNECT_FAILED:
      return "connect-failed";
    case WL_CONNECTION_LOST:
      return "connection-lost";
    case WL_DISCONNECTED:
      return "disconnected";
    default:
      return "unknown";
  }
}

void begin_connection(const Config& config) {
  Serial.printf("[wifi] connecting to SSID \"%s\"\n", config.wifi_ssid);
  WiFi.begin(config.wifi_ssid, config.wifi_password);
  g_last_attempt_ms = millis();
}

// If the configured AP is not visible, list nearby 2.4 GHz networks once. This
// turns a silent "no AP" into an actionable bench/provisioning message.
void log_scan_once(const Config& config) {
  if (g_scanned) {
    return;
  }
  g_scanned = true;

  Serial.println("[wifi] configured AP not found; scanning nearby networks");

  // A scan while an association attempt is in flight returns WIFI_SCAN_FAILED.
  // Power the station down, bring it back up, and let the driver settle so the
  // scan runs from an idle radio.
  WiFi.disconnect(true, false);
  delay(250);
  WiFi.mode(g_status.ap_active ? WIFI_AP_STA : WIFI_STA);
  WiFi.setHostname(config.hostname[0] != '\0' ? config.hostname : "heatpumpsn");
  delay(250);

  const int16_t count = WiFi.scanNetworks(false, true);
  if (count <= 0) {
    Serial.printf("[wifi] scan: no networks found (%d)\n", static_cast<int>(count));
  } else {
    Serial.printf("[wifi] scan: %d network(s)\n", static_cast<int>(count));
    const int16_t shown = count > 12 ? 12 : count;
    for (int16_t i = 0; i < shown; ++i) {
      Serial.printf("[wifi]   %2d  %-32s %4d dBm  ch=%u%s\n", static_cast<int>(i + 1),
                    WiFi.SSID(i).c_str(), WiFi.RSSI(i), static_cast<unsigned>(WiFi.channel(i)),
                    WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "  open" : "");
    }
  }
  WiFi.scanDelete();

  // The scan blocks T_comms for a few seconds; restart the retry interval so
  // the next association attempt is not fired immediately afterwards.
  g_last_attempt_ms = millis();
}

void start_ntp(const Config& config) {
  if (g_ntp_started || !config.ntp_enabled || config.ntp_server[0] == '\0') {
    return;
  }
  const char* timezone = config.timezone[0] != '\0' ? config.timezone : "UTC0";
  configTzTime(timezone, config.ntp_server);
  g_ntp_started = true;
  Serial.printf("[ntp] SNTP started server=\"%s\" tz=\"%s\"\n", config.ntp_server, timezone);
}

void start_ap(const Config& config) {
  if (g_status.ap_active) {
    return;
  }
  const char* ssid = config.ap_ssid[0] != '\0' ? config.ap_ssid : "HeatpumpsN-Setup";
  // WPA2 needs at least 8 characters; never fall back to an open network.
  const char* pass = strlen(config.ap_password) >= 8 ? config.ap_password : kApFallbackPassword;
  WiFi.mode(WIFI_AP_STA);
  if (!WiFi.softAP(ssid, pass)) {
    Serial.println("[wifi] fallback AP failed to start");
    return;
  }
  g_status.ap_active = true;
  g_ap_idle_since_ms = 0;
  char detail[96];
  snprintf(detail, sizeof(detail), "Wi-Fi fallback AP \"%s\" started (portal 192.168.4.1)", ssid);
  logger::log_event(1, 1012, detail);
  Serial.printf("[wifi] %s\n", detail);
}

void stop_ap() {
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  g_status.ap_active = false;
  g_status.ap_clients = 0;
  logger::log_event(0, 1013, "Wi-Fi fallback AP closed");
  Serial.println("[wifi] fallback AP closed");
}

void service_ap(const Config& config, uint32_t now) {
  if (g_status.ap_active) {
    g_status.ap_clients = static_cast<uint8_t>(WiFi.softAPgetStationNum());
  }
  if (g_status.connected) {
    if (!g_status.ap_active) return;
    if (g_status.ap_clients > 0) {
      g_ap_idle_since_ms = 0;
    } else if (g_ap_idle_since_ms == 0) {
      g_ap_idle_since_ms = now;
    } else if (now - g_ap_idle_since_ms >= kApIdleCloseMs) {
      stop_ap();
    }
    return;
  }
  if (g_status.ap_active) return;
  const bool never = g_status.connect_count == 0 && now >= kApAfterBootMs;
  const bool lost = g_down_since_ms != 0 && now - g_down_since_ms >= kApAfterLossMs;
  if (never || lost) start_ap(config);
}

}  // namespace

void begin(const Config& config) {
  g_status = {};
  g_last_wl_status = WL_IDLE_STATUS;
  g_last_attempt_ms = 0;
  g_last_rssi_log_ms = 0;
  g_started = false;
  g_scanned = false;
  g_ntp_started = false;

  if (config.wifi_ssid[0] == '\0') {
    Serial.println("[wifi] no SSID configured; station disabled, opening the setup AP");
    g_started = true;
    start_ap(config);
    return;
  }

  // The controller is mains-powered, so keep the radio awake for a responsive
  // web portal. Credentials are not written to NVS on every association; NVS
  // persistence is the future Wi-Fi manager's job.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(config.hostname[0] != '\0' ? config.hostname : "heatpumpsn");
  // Retry from tick() instead: the core's auto-reconnect races with the
  // explicit begin() calls and logs "STA is connecting" errors.
  WiFi.setAutoReconnect(false);
  WiFi.setSleep(false);

  g_started = true;
  g_status.configured = true;
  begin_connection(config);
}

void tick(const Config& config) {
  if (!g_started) {
    return;
  }

  const uint32_t now = millis();
  service_ap(config, now);
  if (!g_status.configured) {
    return;   // AP only
  }
  const wl_status_t wl = WiFi.status();

  if (wl == WL_CONNECTED) {
    g_down_since_ms = 0;
    if (!g_status.connected) {
      g_status.connected = true;
      g_status.connect_count++;
      g_status.last_connect_ms = now;
      g_status.rssi = static_cast<int8_t>(WiFi.RSSI());
      Serial.printf("[wifi] connected ssid=\"%s\" rssi=%d dBm ip=%s\n",
                    WiFi.SSID().c_str(), static_cast<int>(g_status.rssi),
                    WiFi.localIP().toString().c_str());
      start_ntp(config);
      g_last_rssi_log_ms = now;
    } else if (now - g_last_rssi_log_ms >= kRssiLogIntervalMs) {
      g_status.rssi = static_cast<int8_t>(WiFi.RSSI());
      Serial.printf("[wifi] rssi=%d dBm ip=%s\n", static_cast<int>(g_status.rssi),
                    WiFi.localIP().toString().c_str());
      g_last_rssi_log_ms = now;
    }
  } else {
    if (g_down_since_ms == 0) {
      g_down_since_ms = now == 0 ? 1 : now;
    }
    if (g_status.connected) {
      g_status.connected = false;
      Serial.printf("[wifi] link lost (%s); will retry\n", wl_name(wl));
    }

    if (wl != g_last_wl_status) {
      Serial.printf("[wifi] state: %s\n", wl_name(wl));
    }

    if (wl == WL_NO_SSID_AVAIL && !g_scanned && !g_status.ap_active) {
      log_scan_once(config);
      // The scan cancelled the in-flight attempt; start a fresh one and skip
      // the retry check for this tick (now was sampled before the scan).
      begin_connection(config);
      g_last_wl_status = WiFi.status();
      return;
    }

    // A retry scans other channels and briefly drops AP clients: slow down
    // while someone is using the fallback AP.
    const uint32_t retry = g_status.ap_clients > 0 ? kRetryWithApClientMs : kRetryIntervalMs;
    if (now - g_last_attempt_ms >= retry) {
      begin_connection(config);
    }
  }

  g_last_wl_status = wl;
}

const Status& status() { return g_status; }

const char* state_name() {
  if (!g_status.configured) {
    return "disabled";
  }
  return wl_name(WiFi.status());
}

}  // namespace wifi
}  // namespace hpn
