#include "hpn/logger.h"

namespace hpn {
namespace logger {

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
  // TODO(logger): drain ring buffers, write event log, persist parameters.
  (void)config;
  (void)state;
}

}  // namespace logger
}  // namespace hpn
