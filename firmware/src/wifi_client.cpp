#include "hpn/wifi_client.h"

#include <Arduino.h>
#include <WiFi.h>

namespace hpn {
namespace wifi {
namespace {

constexpr uint32_t kRetryIntervalMs = 10000;
constexpr uint32_t kRssiLogIntervalMs = 60000;
constexpr const char* kHostname = "heatpumpsn";

Status g_status{};
uint32_t g_last_attempt_ms = 0;
uint32_t g_last_rssi_log_ms = 0;
wl_status_t g_last_wl_status = WL_IDLE_STATUS;
bool g_started = false;
bool g_scanned = false;

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
void log_scan_once() {
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
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(kHostname);
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

}  // namespace

void begin(const Config& config) {
  g_status = {};
  g_last_wl_status = WL_IDLE_STATUS;
  g_last_attempt_ms = 0;
  g_last_rssi_log_ms = 0;
  g_started = false;
  g_scanned = false;

  if (config.wifi_ssid[0] == '\0') {
    Serial.println("[wifi] no SSID configured; station disabled");
    return;
  }

  // The controller is mains-powered, so keep the radio awake for a responsive
  // web portal. Credentials are not written to NVS on every association; NVS
  // persistence is the future Wi-Fi manager's job.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(kHostname);
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
  const wl_status_t wl = WiFi.status();

  if (wl == WL_CONNECTED) {
    if (!g_status.connected) {
      g_status.connected = true;
      g_status.connect_count++;
      g_status.last_connect_ms = now;
      g_status.rssi = static_cast<int8_t>(WiFi.RSSI());
      Serial.printf("[wifi] connected ssid=\"%s\" rssi=%d dBm ip=%s\n",
                    WiFi.SSID().c_str(), static_cast<int>(g_status.rssi),
                    WiFi.localIP().toString().c_str());
      g_last_rssi_log_ms = now;
    } else if (now - g_last_rssi_log_ms >= kRssiLogIntervalMs) {
      g_status.rssi = static_cast<int8_t>(WiFi.RSSI());
      Serial.printf("[wifi] rssi=%d dBm ip=%s\n", static_cast<int>(g_status.rssi),
                    WiFi.localIP().toString().c_str());
      g_last_rssi_log_ms = now;
    }
  } else {
    if (g_status.connected) {
      g_status.connected = false;
      Serial.printf("[wifi] link lost (%s); will retry\n", wl_name(wl));
    }

    if (wl != g_last_wl_status) {
      Serial.printf("[wifi] state: %s\n", wl_name(wl));
    }

    if (wl == WL_NO_SSID_AVAIL && !g_scanned) {
      log_scan_once();
      // The scan cancelled the in-flight attempt; start a fresh one and skip
      // the retry check for this tick (now was sampled before the scan).
      begin_connection(config);
      g_last_wl_status = WiFi.status();
      return;
    }

    if (now - g_last_attempt_ms >= kRetryIntervalMs) {
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
