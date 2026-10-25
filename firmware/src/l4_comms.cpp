#include "hpn/l4_comms.h"

#include "hpn/web_portal.h"
#include "hpn/wifi_client.h"

namespace hpn {
namespace l4 {

void begin(const Config& config, SystemState& state) {
  wifi::begin(config);
  web::begin(config, state);
}

void tick(const Config& config, SystemState& state) {
  // Wi-Fi link supervision and the LittleFS/HTTP/WebSocket portal. MQTT and
  // HA command intake are still TODO; the portal's Test Mode is the only
  // command source wired up today.
  wifi::tick(config);
  web::tick(config, state);
}

}  // namespace l4
}  // namespace hpn
