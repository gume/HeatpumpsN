# Runtime checks

End-to-end checks against a **running** controller. They complement the native
unit tests in `firmware/test/`: these exercise the real HTTP API, the RAM log
rings, MQTT discovery/state publishing and the emulation build's LED mapping.

`runtime_check.py` prints one line per check and finishes with either
`PASSED: ...` (exit code 0) or `FAILED: ...` (exit code 1).

## Quick start

```bash
# controller already flashed and reachable
python3 tests/runtime_check.py --base-url http://192.168.2.14

# flash the emulation build, then run every check including the serial boot log
tests/flash_and_check.sh --serial
```

A successful run ends with:

```
PASSED: 45 passed, 0 failed, 0 skipped
```

The exact count changes with the build and the MQTT/emulation availability.

## Prerequisites

| Requirement | Needed for | Notes |
| --- | --- | --- |
| Python 3.9+ | always | standard library only, no `pip install` |
| `mosquitto_sub` (`mosquitto-clients`) | MQTT checks | skipped when `mqtt_host` is empty |
| `pio` + logged-in `pio remote agent` | flashing and `--serial` | `pio remote agent start`, then `pio remote login` |
| `script` (util-linux) | `--serial` | keeps the monitor output free of CR/TTY noise |

The attached bench board is a plain ESP32, so flash with
`HPN_ENV=esp32dev_sim` (the default).

## Options

```
--base-url URL        portal URL (default: $HPN_URL or http://192.168.2.14)
--broker HOST         temporarily point the controller at this broker
--broker-port PORT    port for --broker (0 keeps the configured port)
--serial              reboot under the remote monitor and inspect the boot log
--serial-port DEV     serial device for --serial (default: $HPN_SERIAL_PORT or /dev/ttyUSB0)
```

Environment variables: `HPN_URL`, `HPN_MQTT_HOST`, `HPN_MQTT_PORT`,
`HPN_SERIAL_PORT`, and `HPN_ENV` for the wrapper script.

Examples:

```bash
# verify the deployed configuration as-is
python3 tests/runtime_check.py

# move the controller to a test broker for the run, then restore it
python3 tests/runtime_check.py --broker smcl.tmit.bme.hu --broker-port 31883

# full cycle: build + flash + checks + reboot/boot-log verification
tests/flash_and_check.sh --serial

# also refresh the LittleFS assets
HPN_UPLOAD_FS=1 tests/flash_and_check.sh
```

## What is checked

1. **Status and alive-ness** — `/api/status` is valid, Wi-Fi is connected,
   uptime advances, RAM log capacities are 96 events / 180 slow samples at a
   10 s cadence, and MQTT is connected when enabled.
2. **Portal assets** — `/` and `/js/app.js` are served and contain the
   dashboard LED mirror and the error-text row.
3. **Config contract** — the MQTT log fields exist on `/api/config/network`;
   the obsolete pulse-width fields are gone.
4. **RAM logs** — `/api/log/events` and `/api/log/slow` are populated, stay
   within capacity, have the documented record shape, and no NVS-corruption
   event (501) was latched.
5. **HA discovery** — retained `system_error_code` and `system_error_text`
   discovery payloads exist and the state topics match the configured base
   topic.
6. **MQTT log streaming** — enables streaming with the two full-topic
   overrides, receives a JSON event line and a JSON slow-sample line, then
   restores the original settings.
7. **Error sensors** — an over-temperature warning updates
   `system_error_text` while the numeric code stays `0`; warning recovery,
   a floor return sensor fault (`202`) and a critical mixer sensor fault
   (`201`) each update both topics, and recovery returns to `0` / `No error`.
   The warning text must not contain a double space.
8. **LED mapping** — idle, radiator pumps (LED 3/4 orange), DHW (LED 1 cyan),
   heater (LED 1 orange) and floor flush (LED 2 cyan) states. A critical
   alarm must make LED 0 blink red.
9. **No false diagnostics** — relay/pump transitions must not republish the
   error state topics.
10. **Serial boot (optional)** — the board boots with the saved config, logs
    no Preferences errors, no NVS corruption and no crash markers, then
    reconnects Wi-Fi and MQTT.

## Side effects and restoration

The script is written to be re-runnable on a shared bench:

- It sets safe emulation temperatures (`boiler 45`, `return 30`, `mix 35` °C)
  and all HA intents off, then restores the previous temperatures.
- It clears manual relay overrides for the LED checks and re-applies the ones
  that were manually on.
- It toggles `mqtt_log_enabled` and the two log-topic overrides, then writes
  the original values back (the controller's live setting is what it was
  before the run).
- In the emulation build it briefly sets `boiler_min_off_ms` to `0` so the
  heater can energise immediately, then restores the original value. This is
  the anti-short-cycle protection and is safe to pause on the bench because no
  real heater is attached.
- `--broker` changes `mqtt_host`/`mqtt_port` only for the duration of the run.
- `--serial` **reboots the controller**; run it last.

It never touches Wi-Fi credentials or the NVS config blob beyond the settings
save performed by the normal config PUT endpoint.

## Notes and limitations

- Error-sensor and LED checks require `HPN_IO_EMULATION` (the `esp32dev_sim`
  environment); they are reported as skipped otherwise.
- Counter persistence and anti-seize CRC writes are not exercised on the
  bench board; those are covered by code review and the NVS integrity checks
  in the boot log.
- The MQTT checks use anonymous access. If the broker requires credentials,
  configure them on the controller first and point `--broker`/`--broker-port`
  at the same broker (the script does not change `mqtt_user`/`mqtt_pass`).
- `--serial` needs the remote agent to own the single USB serial port; close
  any other monitor first.
- If the board already latched `501` (real NVS damage), the run will fail the
  "no NVS corruption" check. A clean reboot after fixing the blob clears the
  live flag; see `firmware/README.md`.
