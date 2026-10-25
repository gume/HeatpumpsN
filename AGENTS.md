# ESP32 Development & Verification Instructions

This project targets an ESP32 microcontroller using PlatformIO. The target device is connected to a remote runner running a `pio remote agent`. All host should use this remote mode. Whenever the remote ESP32 is not available, ask the user to make the agent run, login and then run opencode on top of an already authenticated session. Give instructions to the user, how to run the remote agent and how to login.

## Build & Flash Commands
- **Remote flashing:** `pio remote run --target upload`

## Serial Monitor Rules (CRITICAL)
- **NEVER** run `pio remote device monitor` unbounded; it creates an open, blocking terminal stream that will freeze the session.
- To inspect serial boot logs and application output after flashing, **ALWAYS** enforce a timeout:
  ```bash
  timeout 10s pio remote device monitor -b 115200 || true
  ```
