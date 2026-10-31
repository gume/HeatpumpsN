#include "hpn/wifi_client.h"

#include <Arduino.h>
#include <DNSServer.h>
#include <WiFi.h>
#include <string.h>

#include <new>
#include <time.h>

#include "hpn/credentials.h"
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
constexpr const char* kApFallbackPassword = HPN_AP_PASSWORD;

Status g_status{};
// Captive DNS on the fallback AP: every name resolves to the controller, so a
// phone's connectivity probe reaches the portal (web_portal redirects it) and
// the phone offers "Sign in to network" instead of routing over mobile data.
DNSServer g_dns;
uint32_t g_last_attempt_ms = 0;
uint32_t g_last_rssi_log_ms = 0;
wl_status_t g_last_wl_status = WL_IDLE_STATUS;
bool g_started = false;
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

// --- On-demand scan ------------------------------------------------------------
struct Network {
  char ssid[33];
  int8_t rssi;
  uint8_t channel;
  bool secure;
};
constexpr size_t kMaxNetworks = 20;
constexpr uint32_t kScanTimeoutMs = 15000;
portMUX_TYPE g_scan_mux = portMUX_INITIALIZER_UNLOCKED;
// On the heap, allocated by the first scan: the ESP32-S2 has no static DRAM to spare.
Network* g_networks = nullptr;
size_t g_network_count = 0;
volatile bool g_scan_requested = false;
bool g_scan_running = false;
uint32_t g_scan_started_ms = 0;
uint32_t g_scan_done_ms = 0;
const char* g_scan_error = "";

void collect_scan(int16_t count) {
  if (g_networks == nullptr) g_networks = new (std::nothrow) Network[kMaxNetworks];
  Network* found = new (std::nothrow) Network[kMaxNetworks];
  if (g_networks == nullptr || found == nullptr) {
    delete[] found;
    g_scan_error = "out of memory";
    return;
  }
  size_t n = 0;
  for (int16_t i = 0; i < count; ++i) {
    const String ssid = WiFi.SSID(i);
    if (ssid.isEmpty()) continue;   // hidden network
    const int8_t rssi = static_cast<int8_t>(WiFi.RSSI(i));
    size_t k = 0;
    while (k < n && strcmp(found[k].ssid, ssid.c_str()) != 0) ++k;
    if (k < n) {   // same SSID from another access point: keep the strongest
      if (rssi > found[k].rssi) {
        found[k].rssi = rssi;
        found[k].channel = static_cast<uint8_t>(WiFi.channel(i));
      }
      continue;
    }
    if (n == kMaxNetworks) {   // replace the weakest if this one is stronger
      size_t weakest = 0;
      for (size_t m = 1; m < n; ++m) {
        if (found[m].rssi < found[weakest].rssi) weakest = m;
      }
      if (rssi <= found[weakest].rssi) continue;
      k = weakest;
    } else {
      k = n++;
    }
    strncpy(found[k].ssid, ssid.c_str(), sizeof(found[k].ssid) - 1);
    found[k].ssid[sizeof(found[k].ssid) - 1] = '\0';
    found[k].rssi = rssi;
    found[k].channel = static_cast<uint8_t>(WiFi.channel(i));
    found[k].secure = WiFi.encryptionType(i) != WIFI_AUTH_OPEN;
  }
  for (size_t a = 1; a < n; ++a) {   // strongest first
    for (size_t b = a; b > 0 && found[b].rssi > found[b - 1].rssi; --b) {
      const Network t = found[b];
      found[b] = found[b - 1];
      found[b - 1] = t;
    }
  }
  taskENTER_CRITICAL(&g_scan_mux);
  memcpy(g_networks, found, n * sizeof(Network));
  g_network_count = n;
  taskEXIT_CRITICAL(&g_scan_mux);
  delete[] found;
}

void service_scan() {
  if (g_scan_running) {
    const int16_t r = WiFi.scanComplete();
    if (r == WIFI_SCAN_RUNNING && millis() - g_scan_started_ms < kScanTimeoutMs) return;
    if (r >= 0) {
      g_scan_error = "";
      collect_scan(r);
      Serial.printf("[wifi] scan: %d result(s)\n", static_cast<int>(r));
    } else {
      g_scan_error = r == WIFI_SCAN_RUNNING ? "scan timed out" : "scan failed";
      Serial.printf("[wifi] %s\n", g_scan_error);
    }
    WiFi.scanDelete();
    g_scan_running = false;
    g_scan_done_ms = millis();
    g_last_attempt_ms = millis();   // the next station retry waits a full interval
    return;
  }
  if (!g_scan_requested) return;
  g_scan_requested = false;
  if (!g_status.connected && g_status.configured) {
    // A scan cannot run beside an association attempt; stop the attempt
    // (the station is not connected, so nothing is lost).
    WiFi.disconnect(false, false);
  }
  if (WiFi.scanNetworks(true, false) == WIFI_SCAN_FAILED) {
    g_scan_error = "scan failed to start";
    g_scan_done_ms = millis();
    Serial.println("[wifi] scan failed to start");
    return;
  }
  g_scan_running = true;
  g_scan_started_ms = millis();
  g_scan_error = "";
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
  const char* ssid = config.ap_ssid[0] != '\0' ? config.ap_ssid : HPN_AP_SSID;
  // WPA2 needs at least 8 characters; never fall back to an open network.
  const char* pass = strlen(config.ap_password) >= 8 ? config.ap_password : kApFallbackPassword;
  WiFi.mode(WIFI_AP_STA);
  if (!WiFi.softAP(ssid, pass)) {
    Serial.println("[wifi] fallback AP failed to start");
    return;
  }
  g_dns.setErrorReplyCode(DNSReplyCode::NoError);
  if (!g_dns.start(53, "*", WiFi.softAPIP())) {
    Serial.println("[wifi] captive DNS failed to start");
  }
  g_status.ap_active = true;
  g_ap_idle_since_ms = 0;
  char detail[96];
  snprintf(detail, sizeof(detail), "Wi-Fi fallback AP \"%s\" started (portal 192.168.4.1)", ssid);
  logger::log_event(1, 1012, detail);
  Serial.printf("[wifi] %s\n", detail);
}

void stop_ap() {
  g_dns.stop();
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

  service_scan();
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

    // A retry scans other channels and briefly drops AP clients: slow down
    // while someone is using the fallback AP.
    const uint32_t retry = g_status.ap_clients > 0 ? kRetryWithApClientMs : kRetryIntervalMs;
    if (!g_scan_running && now - g_last_attempt_ms >= retry) {
      begin_connection(config);
    }
  }

  g_last_wl_status = wl;
}

const Status& status() { return g_status; }

void request_scan() { g_scan_requested = true; }

void scan_json(JsonObject out) {
  out["running"] = g_scan_running || g_scan_requested;
  out["ageS"] = g_scan_done_ms ? (millis() - g_scan_done_ms) / 1000 : -1;
  out["error"] = g_scan_error;
  JsonArray list = out["networks"].to<JsonArray>();
  Network copy[kMaxNetworks];   // ~720 bytes on the web task's stack
  taskENTER_CRITICAL(&g_scan_mux);
  const size_t n = g_networks != nullptr ? g_network_count : 0;
  if (n) memcpy(copy, g_networks, n * sizeof(Network));
  taskEXIT_CRITICAL(&g_scan_mux);
  for (size_t i = 0; i < n; ++i) {
    JsonObject o = list.add<JsonObject>();
    o["ssid"] = copy[i].ssid;
    o["rssi"] = copy[i].rssi;
    o["channel"] = copy[i].channel;
    o["secure"] = copy[i].secure;
  }
}

const char* state_name() {
  if (!g_status.configured) {
    return "disabled";
  }
  return wl_name(WiFi.status());
}

}  // namespace wifi
}  // namespace hpn
