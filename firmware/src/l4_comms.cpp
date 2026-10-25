#include "hpn/l4_comms.h"

#include "hpn/mqtt_client.h"
#include "hpn/web_portal.h"
#include "hpn/wifi_client.h"

namespace hpn {
namespace l4 {

void begin(const Config& config, SystemState& state) {
  wifi::begin(config);
  mqtt::begin(config, state);
  web::begin(config, state);
}

void tick(const Config& config, SystemState& state) {
  // Wi-Fi link supervision, MQTT/Home Assistant intake, and the
  // LittleFS/HTTP/WebSocket portal.
  wifi::tick(config);
  mqtt::tick(config, state);
  web::tick(config, state);
}

}  // namespace l4
}  // namespace hpn
