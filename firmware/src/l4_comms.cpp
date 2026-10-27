#include "hpn/l4_comms.h"

#include "hpn/led_status.h"
#include "hpn/mqtt_client.h"
#include "hpn/state.h"
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
  // The LED animator only reads, so it gets the published snapshot instead of
  // the struct the real-time core keeps updating.
  SystemState snapshot;
  if (state_snapshot(snapshot)) {
    led::tick(config, snapshot);
  }
}

}  // namespace l4
}  // namespace hpn
