#!/usr/bin/env python3
"""Repeatable runtime checks for the HeatpumpsN controller.

The script talks to a running controller over HTTP and MQTT. It does not build
or flash anything; use ``tests/flash_and_check.sh`` for the flash-and-check
cycle.

What it checks
--------------
* controller reachability, uptime, RAM log capacities and cadence;
* portal static assets and the config API contract;
* RAM event/slow logs and the absence of a latched NVS corruption (501);
* Home Assistant discovery of the three heating-loop switches, the four
  physical binary sensors and the retirement of the old heater/virtual
  entities, plus the retained ``system_error_code`` / ``system_error_text``
  state topics;
* MQTT log streaming, including the two full-topic overrides;
* error-sensor transitions for a warning, warning recovery, a sensor fault
  (202) and a critical mixer sensor fault (201);
* the five-LED mapping in the emulation build;
* that ordinary relay transitions do not update the error sensors;
* the per-circuit keep-alive: a missed repeat command drops the demand while
  keeping the raw intent, raises error 301 and recovers on the next ON;
* the board-owned pump after-run after a heating demand ends;
* optionally, a serial boot check (``--serial``) with the NVS load and
  start-up integrity checks.

The test restores the controller configuration, emulation temperatures and
manual relay overrides it temporarily changes. It never changes Wi-Fi
settings. In the emulation build it briefly zeroes `boiler_min_off_ms` so the
heater can be verified, disables the pump after-run for the relay/LED checks,
and shortens `demand_timeout_s` for the keep-alive check; every one of those
values is restored.

Requirements
------------
* Python 3.9+ (standard library only);
* ``mosquitto_sub`` from ``mosquitto-clients`` for the MQTT checks;
* a controller with MQTT configured for those checks (they are skipped when
  the broker host is empty);
* for ``--serial``: PlatformIO with a logged-in ``pio remote`` agent.

Exit codes: 0 = all checks passed (skips allowed), 1 = at least one failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

DEFAULT_URL = os.environ.get("HPN_URL", "http://192.168.2.14")
DEFAULT_SERIAL_PORT = os.environ.get("HPN_SERIAL_PORT", "/dev/ttyUSB0")
DEFAULT_BROKER = os.environ.get("HPN_MQTT_HOST", "")
DEFAULT_BROKER_PORT = int(os.environ.get("HPN_MQTT_PORT", "1883"))


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"[ PASS ] {name}" + (f"  ({detail})" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        print(f"[ FAIL ] {name}" + (f"  ({detail})" if detail else ""))

    def skip(self, name: str, detail: str = "") -> None:
        self.skipped += 1
        print(f"[ SKIP ] {name}" + (f"  ({detail})" if detail else ""))

    def expect(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)
        return condition

    def finish(self) -> int:
        print()
        if self.failed:
            print(
                f"FAILED: {self.passed} passed, {self.failed} failed, "
                f"{self.skipped} skipped"
            )
            return 1
        print(f"PASSED: {self.passed} passed, 0 failed, {self.skipped} skipped")
        return 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class HttpFailure(RuntimeError):
    pass


def http_request(
    base: str,
    path: str,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def http_json(
    base: str,
    path: str,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 8.0,
) -> Any:
    status, payload, _ = http_request(base, path, method, body, timeout)
    text = payload.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError as error:
        raise HttpFailure(f"{method} {path}: invalid JSON ({error}): {text[:200]}") from error
    if status < 200 or status >= 300:
        raise HttpFailure(f"{method} {path}: HTTP {status}: {text[:300]}")
    return parsed


def http_text(base: str, path: str, timeout: float = 8.0) -> tuple[int, str]:
    status, payload, _ = http_request(base, path, timeout=timeout)
    return status, payload.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# MQTT helpers (mosquitto_sub, so only mosquitto-clients is required)
# ---------------------------------------------------------------------------


def mqtt_available() -> bool:
    return shutil.which("mosquitto_sub") is not None


def parse_sub_output(stdout: str) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for raw in stdout.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith(("Client ", "Subscribed (", "Timed out")):
            continue
        topic, separator, payload = line.partition(" ")
        if not separator:
            continue
        messages.append((topic, payload))
    return messages


class Mqtt:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def _command(self, topics: Iterable[str], wait: float, count: int | None) -> list[str]:
        command = [
            "mosquitto_sub",
            "-h",
            self.host,
            "-p",
            str(self.port),
            "-v",
            "-W",
            str(max(1, int(wait))),
        ]
        if count is not None:
            command += ["-C", str(count)]
        for topic in topics:
            command += ["-t", topic]
        return command

    def run(
        self, topics: Iterable[str], wait: float = 4.0, count: int | None = None
    ) -> tuple[list[tuple[str, str]], str]:
        try:
            process = subprocess.run(
                self._command(topics, wait, count),
                capture_output=True,
                text=True,
                timeout=wait + 8.0,
            )
        except subprocess.TimeoutExpired:
            return [], "mosquitto_sub did not exit"
        return parse_sub_output(process.stdout), process.stderr.strip()

    def start(self, topics: Iterable[str], wait: float = 60.0) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self._command(topics, wait, None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def stop(process: subprocess.Popen[str]) -> tuple[list[tuple[str, str]], str]:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=6.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        return parse_sub_output(stdout), stderr.strip()

    def retained(self, topics: Iterable[str], wait: float = 4.0) -> list[tuple[str, str]]:
        messages, _ = self.run(topics, wait=wait)
        return messages


# ---------------------------------------------------------------------------
# Controller checks
# ---------------------------------------------------------------------------

SAFE_TEMPERATURES = {"boiler": 45.0, "return": 30.0, "mix": 35.0}


def same_color(actual: list[int], expected: tuple[int, int, int]) -> bool:
    return len(actual) == 3 and tuple(int(v) for v in actual) == expected


def cyanish(actual: list[int]) -> bool:
    # The flush animation ramps cyan between 32 and 220 brightness, so the
    # trough is about (0, 18, 18).
    return (
        len(actual) == 3
        and actual[0] < 20
        and actual[1] >= 15
        and actual[2] >= 15
        and abs(actual[1] - actual[2]) <= 40
    )


class Controller:
    def __init__(self, args: argparse.Namespace, checker: Checker) -> None:
        self.base = args.base_url.rstrip("/")
        self.args = args
        self.checker = checker
        self.status: dict[str, Any] = {}
        self.network: dict[str, Any] = {}
        self.safety: dict[str, Any] = {}
        self.emulation = False
        self.base_topic = ""
        self.mqtt: Mqtt | None = None
        self.original_network: dict[str, Any] = {}
        self.changed_network: set[str] = set()
        self.original_safety: dict[str, Any] = {}
        self.changed_safety: set[str] = set()
        self.hardware: dict[str, Any] = {}
        self.original_hardware: dict[str, Any] = {}
        self.changed_hardware: set[str] = set()
        self.test_was_active = False
        self.original_temperatures: list[dict[str, Any]] = []
        self.original_manual_relays: dict[str, bool] = {}
        self.restored = False
        self.firmware_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "firmware")
        )

    # -- setup / teardown -------------------------------------------------

    def connect(self) -> bool:
        try:
            self.status = http_json(self.base, "/api/status")
        except Exception as error:  # noqa: BLE001 - report any connection failure
            self.checker.fail("controller reachable", str(error))
            return False
        self.checker.ok(
            "controller reachable",
            f"{self.status.get('hostname', '?')} / {self.status.get('chip', '?')}",
        )

        try:
            self.network = http_json(self.base, "/api/config/network")
            self.original_network = dict(self.network)
        except Exception as error:  # noqa: BLE001
            self.checker.fail("network config readable", str(error))
            self.network = {}

        try:
            self.safety = http_json(self.base, "/api/config/safety")
            self.original_safety = dict(self.safety)
        except Exception as error:  # noqa: BLE001
            self.checker.fail("safety config readable", str(error))
            self.safety = {}

        try:
            self.hardware = http_json(self.base, "/api/config/hardware")
            self.original_hardware = dict(self.hardware)
        except Exception as error:  # noqa: BLE001
            self.checker.fail("hardware config readable", str(error))
            self.hardware = {}

        try:
            self.test_was_active = bool(
                http_json(self.base, "/api/state").get("test", {}).get("active")
            )
        except Exception:  # noqa: BLE001 - optional
            self.test_was_active = False

        self.emulation = bool(self.status.get("portal", {}).get("emulation"))
        self.base_topic = str(self.network.get("mqtt_base_topic", "")) or "heatpumpsn/controller"

        if self.args.broker:
            patch: dict[str, Any] = {"mqtt_host": self.args.broker}
            if self.args.broker_port:
                patch["mqtt_port"] = self.args.broker_port
            if self._changed_from_original(patch):
                self._put_network(patch)
                self.checker.ok("temporary MQTT broker configured", self._broker_label())

        host = str(self.network.get("mqtt_host", ""))
        port = int(self.network.get("mqtt_port", 1883))
        if host:
            self.mqtt = Mqtt(host, port)
        return True

    def _broker_label(self) -> str:
        return f"{self.network.get('mqtt_host', '?')}:{self.network.get('mqtt_port', '?')}"

    def _changed_from_original(self, patch: dict[str, Any]) -> bool:
        for key, value in patch.items():
            if key in self.original_network and self.original_network[key] != value:
                return True
            if key not in self.original_network:
                return True
        return False

    def _put_network(self, patch: dict[str, Any], record: bool = True) -> dict[str, Any]:
        response = http_json(self.base, "/api/config/network", "PUT", patch)
        if record:
            self.changed_network.update(patch.keys())
        # Keep the local copy in sync so later checks use the new values.
        self.network.update(patch)
        return response

    def _put_safety(self, patch: dict[str, Any], record: bool = True) -> dict[str, Any]:
        response = http_json(self.base, "/api/config/safety", "PUT", patch)
        if record:
            self.changed_safety.update(patch.keys())
        self.safety.update(patch)
        return response

    def _put_hardware(self, patch: dict[str, Any], record: bool = True) -> dict[str, Any]:
        response = http_json(self.base, "/api/config/hardware", "PUT", patch)
        if record:
            self.changed_hardware.update(patch.keys())
        self.hardware.update(patch)
        return response

    def prepare(self) -> None:
        if not self.emulation:
            return
        try:
            snapshot = http_json(self.base, "/api/sim")
            self.original_temperatures = list(snapshot.get("temperatures", []))
            self.original_manual_relays = {
                name: bool(on)
                for name, on in snapshot.get("relayManual", {}).items()
            }
            # Manual relay overrides would keep an LED lit regardless of the
            # simulated HA intent, so clear them for the LED checks.
            http_request(self.base, "/api/sim/overrides/clear", "POST")
            # The board now owns the pump after-run. Disable it while the
            # relay/LED checks expect an immediate return to idle; the
            # dedicated after-run check re-enables it and the final restore
            # puts the original value back.
            if int(self.safety.get("pump_stop_after_heating_s", 0) or 0) != 0:
                self._put_safety({"pump_stop_after_heating_s": 0})
            for role, value in SAFE_TEMPERATURES.items():
                self._set_temperature(role, value, True, quiet=True)
            self._set_intents({}, quiet=True)
            time.sleep(2.5)
        except Exception as error:  # noqa: BLE001
            self.checker.fail("emulation baseline prepared", str(error))

    def restore(self) -> None:
        if self.restored:
            return
        self.restored = True
        if self.emulation:
            try:
                self._set_intents({}, quiet=True)
                http_request(self.base, "/api/sim/overrides/clear", "POST")
                for name, on in self.original_manual_relays.items():
                    if on:
                        http_json(
                            self.base,
                            "/api/sim/relay",
                            "POST",
                            {"relay": name, "on": True},
                        )
                for reading in self.original_temperatures:
                    role = reading.get("role")
                    if role:
                        self._set_temperature(
                            role,
                            float(reading.get("value", 0.0)),
                            bool(reading.get("valid", True)),
                            quiet=True,
                        )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        if self.changed_network and self.original_network:
            patch = {
                key: self.original_network[key]
                for key in self.changed_network
                if key in self.original_network
            }
            if patch:
                try:
                    self._put_network(patch, record=False)
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore network config: {error}")
        if self.changed_safety and self.original_safety:
            patch = {
                key: self.original_safety[key]
                for key in self.changed_safety
                if key in self.original_safety
            }
            if patch:
                try:
                    self._put_safety(patch, record=False)
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore safety config: {error}")
        if self.changed_hardware and self.original_hardware:
            patch = {
                key: self.original_hardware[key]
                for key in self.changed_hardware
                if key in self.original_hardware
            }
            if patch:
                try:
                    self._put_hardware(patch, record=False)
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore hardware config: {error}")

    # -- emulation helpers ------------------------------------------------

    def _set_temperature(
        self, role: str, value: float, valid: bool, quiet: bool = False
    ) -> dict[str, Any]:
        result = http_json(
            self.base,
            "/api/sim/temperature",
            "POST",
            {"role": role, "value": value, "valid": valid},
        )
        if not quiet:
            self.checker.ok(
                f"sim temperature {role}={value:.1f} valid={valid}",
            )
        return result

    def _set_intents(self, intents: dict[str, Any], quiet: bool = False) -> dict[str, Any]:
        payload = {
            "heating_loop_1": False,
            "heating_loop_2": False,
            "floor_heating": False,
            "dhw_active": False,
        }
        payload.update(intents)
        result = http_json(self.base, "/api/sim/intent", "POST", payload)
        if not quiet:
            self.checker.ok("sim intents applied", json.dumps(payload, sort_keys=True))
        return result

    def _set_test_output(self, output: str, on: bool, quiet: bool = False) -> dict[str, Any]:
        result = http_json(
            self.base, "/api/test/command", "POST", {"output": output, "on": on}
        )
        if not quiet:
            self.checker.ok(f"test output {output}={'on' if on else 'off'}")
        return result

    def _live_state(self) -> dict[str, Any]:
        return http_json(self.base, "/api/state")

    def _circuit(self, state: dict[str, Any], name: str) -> dict[str, Any]:
        return state.get("circuits", {}).get(name, {})

    def _leds(self) -> list[list[int]]:
        snapshot = http_json(self.base, "/api/sim")
        return [list(map(int, color)) for color in snapshot.get("leds", [])]

    # -- checks -----------------------------------------------------------

    def run_checks(self) -> None:
        self.check_status()
        self.check_assets()
        self.check_config_contract()
        self.check_ram_logs()
        if self.mqtt is not None:
            self.check_mqtt_discovery()
            self.check_mqtt_error_states()
            self.check_log_streaming()
        else:
            self.checker.skip("MQTT discovery", "mqtt_host is empty")
            self.checker.skip("MQTT error states", "mqtt_host is empty")
            self.checker.skip("MQTT log streaming", "mqtt_host is empty")

        if self.emulation:
            self.check_output_driver()
            self.check_error_sensors()
            self.check_leds()
            self.check_keepalive()
            self.check_after_run()
            self.check_mixer_cap()
            self.check_sensor_roles()
        else:
            self.checker.skip("error-sensor fault transitions", "requires HPN_IO_EMULATION")
            self.checker.skip("LED mapping", "requires HPN_IO_EMULATION")
            self.checker.skip("keep-alive timeout", "requires HPN_IO_EMULATION")
            self.checker.skip("board after-run", "requires HPN_IO_EMULATION")
            self.checker.skip("mixer opening cap enforcement", "requires HPN_IO_EMULATION")
            self.checker.skip("sensor role verification", "requires HPN_IO_EMULATION")

    def check_status(self) -> None:
        status = self.status
        self.checker.expect("status ok", bool(status.get("ok")))
        self.checker.expect(
            "Wi-Fi connected",
            bool(status.get("wifi", {}).get("connected")),
            str(status.get("wifi", {}).get("ip", "")),
        )
        portal = status.get("portal", {})
        self.checker.expect("event log capacity 96", portal.get("eventLogSize") == 96)
        self.checker.expect("slow log capacity 180", portal.get("slowLogSize") == 180)
        self.checker.expect("slow log cadence 10 s", portal.get("slowCadenceS") == 10)

        if status.get("mqtt", {}).get("enabled"):
            self.checker.expect(
                "MQTT connected",
                bool(status.get("mqtt", {}).get("connected")),
                self._broker_label(),
            )

        first = int(status.get("uptime", 0))
        time.sleep(2.2)
        second_status = http_json(self.base, "/api/status")
        second = int(second_status.get("uptime", 0))
        self.checker.expect("uptime advances", second > first, f"{first} -> {second}")

    def check_assets(self) -> None:
        try:
            status, page = http_text(self.base, "/")
            self.checker.expect("portal index served", status == 200 and "HeatpumpsN" in page)
            status, script = http_text(self.base, "/js/app.js")
            self.checker.expect(
                "dashboard JS served",
                status == 200 and "dashboardLeds" in script and "Error text" in script,
            )
            self.checker.expect(
                "portal JS exposes the new safety features",
                status == 200
                and "verify-sensor-roles" in script
                and "Pump current calibration" in script
                and "mix_min_open_pct" in script,
            )
        except Exception as error:  # noqa: BLE001
            self.checker.fail("portal static assets", str(error))

    def check_config_contract(self) -> None:
        required = ("mqtt_log_enabled", "mqtt_log_slow_topic", "mqtt_log_event_topic")
        missing = [key for key in required if key not in self.network]
        self.checker.expect(
            "MQTT log config fields exposed",
            not missing,
            "missing " + ", ".join(missing) if missing else "",
        )
        safety_fields = (
            "mix_max_open_pct",
            "mix_min_open_pct",
            "mix_max_dynamic",
            "mix_max_below_pct",
            "mix_max_above_pct",
            "mix_max_switch_c",
            "mix_max_hysteresis_c",
            "mix_overshoot_delta_c",
            "mix_overshoot_recovery_c",
            "mix_overshoot_target_pct",
            "sensor_role_check_enabled",
            "sensor_role_tolerance_c",
            "dhw_infer_enabled",
            "dhw_infer_drop_c",
            "dhw_infer_window_s",
            "dhw_infer_recovery_c",
            "dhw_infer_max_s",
        )
        missing_safety = [key for key in safety_fields if key not in self.safety]
        self.checker.expect(
            "mixer window and supervision fields exposed",
            not missing_safety,
            "missing " + ", ".join(missing_safety) if missing_safety else "",
        )
        hardware_fields = (
            "pump_current_w_per_v",
            "pump_current_offset_v",
            "pump_power_check_enabled",
        )
        missing_hardware = [key for key in hardware_fields if key not in self.hardware]
        self.checker.expect(
            "pump current calibration fields exposed",
            not missing_hardware,
            "missing " + ", ".join(missing_hardware) if missing_hardware else "",
        )
        expander_fields = (
            "output_expander",
            "expander_address",
            "expander_data_pin",
            "expander_clock_pin",
            "expander_latch_pin",
            "expander_oe_pin",
            "expander_oe_active_low",
            "expander_bit_heater",
            "expander_bit_pump_floor",
            "expander_bit_mix_direction",
            "expander_bit_mux_s1",
            "expander_bit_mix_fault",
            "mix_fault_active_low",
            "output_active_low_heater",
            "output_active_low_pump_floor",
            "output_active_low_mix_motion",
            "current_sense",
            "ads1115_address",
            "tlv9064_present",
        )
        missing_expander = [key for key in expander_fields if key not in self.hardware]
        self.checker.expect(
            "output driver and polarity fields exposed",
            not missing_expander,
            "missing " + ", ".join(missing_expander) if missing_expander else "",
        )
        roles_ok = False
        try:
            roles = http_json(self.base, "/api/sensors/roles")
            roles_ok = {"measurable", "plausible", "summary", "checks"} <= set(roles)
        except Exception:  # noqa: BLE001 - reported by the check below
            roles_ok = False
        self.checker.expect("sensor role endpoint exposes the contract", roles_ok)

        removed = ("min_pulse_ms", "max_pulse_ms", "pulse_cooldown_ms")
        found: list[str] = []
        for section in ("safety", "hardware", "system"):
            try:
                payload = json.dumps(http_json(self.base, f"/api/config/{section}"))
            except Exception:  # noqa: BLE001
                continue
            found += [key for key in removed if key in payload]
        self.checker.expect("obsolete pulse fields removed", not found, ", ".join(found))

    def check_output_driver(self) -> None:
        """The port-expander selection, address and polarity round-trip."""
        patch = {
            "output_expander": 2,  # PCF8574
            "expander_address": 0x27,
            "i2c_sda": 8,
            "i2c_scl": 10,
            # HomeNet Heating control v1.x bit map, with mixed output polarity.
            "expander_bit_pump_rad1": 0,
            "expander_bit_pump_rad2": 1,
            "expander_bit_pump_floor": 2,
            "expander_bit_mix_direction": 3,
            "expander_bit_mix_motion": 4,
            "expander_bit_heater": 5,
            "expander_bit_mix_fault": 7,
            "mix_fault_active_low": True,
            "output_active_low_heater": False,
            "output_active_low_pump_floor": True,
            "output_active_low_pump_rad1": True,
            "output_active_low_pump_rad2": True,
            "output_active_low_mix_direction": True,
            "output_active_low_mix_motion": True,
            # No analogue MUX on this board; ADS1115 with the optional sensor.
            "expander_bit_mux_s0": 255,
            "expander_bit_mux_s1": 255,
            "current_sense": 2,
            "ads1115_address": 0x48,
            "tlv9064_present": True,
        }
        response = self._put_hardware(patch)
        stored = http_json(self.base, "/api/config/hardware")
        mismatched = {
            key: (stored.get(key), value)
            for key, value in patch.items()
            if stored.get(key) != value
        }
        self.checker.expect(
            "PCF8574 driver, per-output polarity and current front end round-trip",
            not mismatched,
            json.dumps(mismatched, sort_keys=True) if mismatched else "",
        )
        self.checker.expect(
            "output-driver change requests a reboot",
            bool(response.get("reboot_required")),
            json.dumps(response, sort_keys=True),
        )

        duplicate = dict(patch)
        duplicate["expander_bit_heater"] = 1  # already used by the floor pump
        status, payload, _ = http_request(self.base, "/api/config/hardware", "PUT", duplicate)
        text = payload.decode("utf-8", "replace")
        self.checker.expect(
            "duplicate expander bits rejected",
            status == 400 and "unique" in text,
            f"HTTP {status}: {text[:160]}",
        )

    def check_ram_logs(self) -> None:
        events = http_json(self.base, "/api/log/events")
        entries = events.get("entries", [])
        self.checker.expect("RAM event log populated", len(entries) > 0, f"{len(entries)} entries")
        self.checker.expect("event log within capacity", len(entries) <= 96)
        fields_ok = all(
            {"seq", "ts", "severity", "code", "detail"} <= set(entry) for entry in entries
        )
        self.checker.expect("event log record shape", fields_ok)
        corrupted = [entry for entry in entries if int(entry.get("code", -1)) == 501]
        self.checker.expect("no NVS corruption event (501)", not corrupted)

        samples = http_json(self.base, "/api/log/slow")
        # The first sample is written on the 10 s cadence. A freshly flashed
        # board may not have produced one yet when the checks start.
        deadline = time.monotonic() + 12.0
        while not samples.get("samples") and time.monotonic() < deadline:
            time.sleep(1.0)
            samples = http_json(self.base, "/api/log/slow")
        slow = samples.get("samples", [])
        self.checker.expect("RAM slow log populated", len(slow) > 0, f"{len(slow)} samples")
        self.checker.expect("slow log within capacity", len(slow) <= 180)
        self.checker.expect("slow log cadence 10 s", samples.get("cadence_s") == 10)
        shape_ok = all(
            {"ts", "boiler", "mix", "ret", "pos", "relays"} <= set(sample) for sample in slow
        )
        self.checker.expect("slow log record shape", shape_ok)

    def check_mqtt_discovery(self) -> None:
        assert self.mqtt is not None
        # Discover every entity and filter by our own state-topic prefix. The
        # broker may also carry retained discovery from other devices.
        messages = self.mqtt.retained(["homeassistant/+/+/+/config"], wait=5.0)
        state_prefix = self.base_topic + "/state/"
        entities: dict[tuple[str, str], dict[str, Any]] = {}
        for topic, payload in messages:
            parts = topic.split("/")
            if len(parts) != 5 or parts[0] != "homeassistant" or parts[4] != "config":
                continue
            key = (parts[1], parts[3])
            if not payload.strip():
                # Empty retained payload = Home Assistant deletes the entity.
                continue
            try:
                entity = json.loads(payload)
            except json.JSONDecodeError:
                continue
            state_topic = str(entity.get("state_topic", ""))
            if state_topic.startswith(state_prefix):
                entities[key] = entity

        switches = {
            "heating_loop_1": "Heating loop 1",
            "heating_loop_2": "Heating loop 2",
            "floor_heating": "Floor heating",
        }
        for object_id, name in switches.items():
            entity = entities.get(("switch", object_id))
            ok = (
                bool(entity)
                and entity.get("name") == name
                and entity.get("state_topic") == f"{state_prefix}{object_id}"
                and entity.get("command_topic") == f"{self.base_topic}/cmd/{object_id}"
            )
            self.checker.expect(
                f"HA discovery switch {object_id}",
                ok,
                str(entity.get("name", "")) if entity else "retained config not found",
            )

        binary_sensors = {
            "pump_1": ("Pump 1", "pump_1_physical"),
            "pump_2": ("Pump 2", "pump_2_physical"),
            "floor_pump": ("Floor pump", "floor_pump_physical"),
            "heater": ("Heater", "heater_physical"),
        }
        for object_id, (name, state_key) in binary_sensors.items():
            entity = entities.get(("binary_sensor", object_id))
            ok = (
                bool(entity)
                and entity.get("name") == name
                and entity.get("state_topic") == f"{state_prefix}{state_key}"
            )
            self.checker.expect(
                f"HA discovery binary sensor {object_id}",
                ok,
                str(entity.get("name", "")) if entity else "retained config not found",
            )

        switch_ids = {key for key in entities if key[0] == "switch"}
        binary_ids = {key for key in entities if key[0] == "binary_sensor"}
        self.checker.expect(
            "HA exposes exactly three switches (no heater switch)",
            switch_ids == {("switch", object_id) for object_id in switches},
            ", ".join(sorted(object_id for _, object_id in switch_ids)),
        )
        self.checker.expect(
            "HA exposes exactly four physical binary sensors",
            binary_ids == {("binary_sensor", object_id) for object_id in binary_sensors},
            ", ".join(sorted(object_id for _, object_id in binary_ids)),
        )

        for object_id in ("system_error_code", "system_error_text"):
            entity = entities.get(("sensor", object_id))
            ok = (
                bool(entity)
                and bool(entity.get("name"))
                and entity.get("state_topic") == f"{state_prefix}{object_id}"
            )
            self.checker.expect(
                f"HA discovery {object_id}",
                ok,
                str(entity.get("name", "")) if entity else "retained config not found",
            )

        obsolete = [
            ("switch", "heater"),
            ("switch", "pump_floor"),
            ("switch", "pump_rad1"),
            ("switch", "pump_rad2"),
            ("binary_sensor", "heater_virtual"),
            ("binary_sensor", "heater_physical"),
            ("binary_sensor", "pump_floor_virtual"),
            ("binary_sensor", "pump_floor_physical"),
            ("binary_sensor", "pump_rad1_physical"),
            ("binary_sensor", "pump_rad2_physical"),
        ]
        stale = [
            f"{component}/{object_id}"
            for component, object_id in obsolete
            if (component, object_id) in entities
        ]
        self.checker.expect(
            "retired heater/virtual entities removed from discovery",
            not stale,
            ", ".join(stale),
        )

    def check_mqtt_error_states(self) -> None:
        assert self.mqtt is not None
        code_topic = f"{self.base_topic}/state/system_error_code"
        text_topic = f"{self.base_topic}/state/system_error_text"
        messages = self.mqtt.retained([code_topic, text_topic], wait=4.0)
        values = {topic: payload for topic, payload in messages}
        code = values.get(code_topic)
        text = values.get(text_topic)
        self.checker.expect(
            "error code state readable",
            code is not None and code.strip().isdigit(),
            str(code),
        )
        self.checker.expect(
            "error text state readable",
            bool(text),
            str(text),
        )

    def check_log_streaming(self) -> None:
        assert self.mqtt is not None
        original = {
            "mqtt_log_enabled": bool(self.network.get("mqtt_log_enabled")),
            "mqtt_log_event_topic": str(self.network.get("mqtt_log_event_topic", "")),
            "mqtt_log_slow_topic": str(self.network.get("mqtt_log_slow_topic", "")),
        }
        event_topic = f"{self.base_topic}/test/log/event"
        slow_topic = f"{self.base_topic}/test/log/slow"
        subscriber = self.mqtt.start([event_topic, slow_topic], wait=25.0)
        try:
            self._put_network(
                {
                    "mqtt_log_enabled": True,
                    "mqtt_log_event_topic": event_topic,
                    "mqtt_log_slow_topic": slow_topic,
                }
            )
            # Wait for one 10-second slow sample, and let the settings-save
            # event (published after the config write) reach the event topic.
            time.sleep(13.0)
        finally:
            messages, stderr = Mqtt.stop(subscriber)
            self._put_network(original, record=False)

        events = [
            (topic, payload)
            for topic, payload in messages
            if topic == event_topic
        ]
        slows = [
            (topic, payload)
            for topic, payload in messages
            if topic == slow_topic
        ]
        event_ok = False
        for _, payload in events:
            try:
                record = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "seq" in record and "detail" in record:
                event_ok = True
                break
        slow_ok = False
        for _, payload in slows:
            try:
                record = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "seq" in record and "boiler" in record and "relays" in record:
                slow_ok = True
                break
        self.checker.expect(
            "MQTT event log override",
            event_ok,
            f"{len(events)} messages" + (f"; {stderr}" if stderr else ""),
        )
        self.checker.expect("MQTT slow log override", slow_ok, f"{len(slows)} messages")

    def _fault_wait(self) -> float:
        threshold = int(self.safety.get("sensor_error_threshold", 3) or 3)
        return max(4.0, threshold * 0.6 + 3.0)

    def check_error_sensors(self) -> None:
        assert self.mqtt is not None
        code_topic = f"{self.base_topic}/state/system_error_code"
        text_topic = f"{self.base_topic}/state/system_error_text"

        for role, value in SAFE_TEMPERATURES.items():
            self._set_temperature(role, value, True, quiet=True)
        time.sleep(2.0)

        subscriber = self.mqtt.start([code_topic, text_topic], wait=70.0)
        # Let the retained baseline ("0" / "No error") arrive before any new
        # state is published, so the code-0 assertion has something to match.
        time.sleep(1.5)
        led_red_seen = False
        led_off_seen = False
        try:
            # Warning, with the numeric code staying at 0.
            self._set_temperature("mix", 46.0, True, quiet=True)
            time.sleep(4.0)
            self._set_temperature("mix", 35.0, True, quiet=True)
            time.sleep(4.0)

            # Persistent return-sensor fault -> 202.
            self._set_temperature("return", 30.0, False, quiet=True)
            time.sleep(self._fault_wait())
            self._set_temperature("return", 30.0, True, quiet=True)
            time.sleep(4.0)

            # Critical mixer-sensor fault -> 201, and LED 0 must blink red.
            self._set_temperature("mix", 35.0, False, quiet=True)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                led = self._leds()[0]
                if led == [220, 0, 0]:
                    led_red_seen = True
                if led == [0, 0, 0]:
                    led_off_seen = True
                if led_red_seen and led_off_seen:
                    break
                time.sleep(0.1)
            # The RAM logger samples diagnostic transitions once per second. The
            # LED blink is proven in well under a second, so hold the fault long
            # enough for at least one logger tick to record the 201 transition.
            time.sleep(2.5)
            self._set_temperature("mix", 35.0, True, quiet=True)
            time.sleep(4.0)
        finally:
            messages, stderr = Mqtt.stop(subscriber)

        codes = [payload for topic, payload in messages if topic == code_topic]
        texts = [payload for topic, payload in messages if topic == text_topic]
        warning_texts = [text for text in texts if text.startswith("Warning:")]
        self.checker.expect(
            "warning text updates error sensor",
            bool(warning_texts) and any("over-temperature" in text for text in warning_texts),
            warning_texts[0] if warning_texts else "no warning text",
        )
        self.checker.expect(
            "warning text has no double space",
            all("Warning:  " not in text for text in texts),
        )
        self.checker.expect("warning keeps numeric code 0", "0" in codes, ",".join(codes))
        self.checker.expect(
            "sensor fault sets code 202",
            "202" in codes,
            ",".join(codes) + (f"; {stderr}" if stderr else ""),
        )
        self.checker.expect(
            "sensor fault text includes 202",
            any(text.startswith("202:") for text in texts),
        )
        self.checker.expect("critical fault sets code 201", "201" in codes)
        self.checker.expect(
            "critical fault text includes 201",
            any(text.startswith("201:") for text in texts),
        )
        self.checker.expect(
            "error sensors recover to healthy",
            codes and codes[-1].strip() == "0" and "No error" in texts,
            f"last code {codes[-1] if codes else '?'}",
        )
        self.checker.expect(
            "critical alarm blinks LED 0 red",
            led_red_seen and led_off_seen,
            f"red={led_red_seen} off={led_off_seen}",
        )

        entries = http_json(self.base, "/api/log/events").get("entries", [])
        event_codes = {int(entry.get("code", -1)) for entry in entries}
        self.checker.expect("RAM log records 202 transition", 202 in event_codes)
        self.checker.expect("RAM log records 201 transition", 201 in event_codes)

    def check_leds(self) -> None:
        assert self.mqtt is not None
        code_topic = f"{self.base_topic}/state/system_error_code"
        text_topic = f"{self.base_topic}/state/system_error_text"

        # Subscribing before the relay transitions lets us prove that they do
        # not publish new diagnostic state.
        subscriber = self.mqtt.start([code_topic, text_topic], wait=120.0)
        try:
            time.sleep(1.5)  # consume the retained baseline messages
            self._set_intents({}, quiet=True)
            time.sleep(3.0)
            idle = self._leds()
            self.checker.expect(
                "LED idle mapping",
                len(idle) == 5
                and same_color(idle[0], (0, 160, 0))
                and all(same_color(color, (0, 0, 0)) for color in idle[1:]),
                str(idle),
            )

            self._set_intents({"heating_loop_1": True, "heating_loop_2": True}, quiet=True)
            time.sleep(3.5)
            rads = self._leds()
            self.checker.expect(
                "LED 3/4 radiator pumps orange",
                same_color(rads[3], (200, 70, 0)) and same_color(rads[4], (200, 70, 0)),
                str(rads[3:5]),
            )

            self._set_intents({"dhw_active": True}, quiet=True)
            time.sleep(3.5)
            dhw = self._leds()
            self.checker.expect("LED 1 DHW cyan", same_color(dhw[1], (0, 150, 150)), str(dhw[1]))

            # The heater is gated by boiler_min_off_ms (anti-short-cycle). On a
            # freshly booted or recently cycled board that window would block
            # the heater for up to three minutes. The emulation build has no
            # real heater, so pause the window for this check and restore it.
            paused_window = False
            if int(self.safety.get("boiler_min_off_ms", 0) or 0) > 0:
                try:
                    self._put_safety({"boiler_min_off_ms": 0})
                    paused_window = True
                    self.checker.ok("anti-short-cycle window paused for LED test")
                except Exception as error:  # noqa: BLE001
                    self.checker.fail(
                        "anti-short-cycle window paused for LED test", str(error)
                    )

            self._set_intents({"floor_heating": True}, quiet=True)
            deadline = time.monotonic() + 12.0
            heating = self._leds()
            while time.monotonic() < deadline:
                heating = self._leds()
                if len(heating) > 1 and same_color(heating[1], (200, 70, 0)):
                    break
                time.sleep(0.5)
            self.checker.expect(
                "LED 1 heater orange",
                same_color(heating[1], (200, 70, 0)),
                str(heating[1]),
            )
            self.checker.expect(
                "LED 2 floor pump active",
                cyanish(heating[2]) or same_color(heating[2], (0, 160, 0)),
                str(heating[2]),
            )

            if paused_window:
                try:
                    self._put_safety(
                        {"boiler_min_off_ms": self.original_safety["boiler_min_off_ms"]},
                        record=False,
                    )
                    self.changed_safety.discard("boiler_min_off_ms")
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore boiler_min_off_ms: {error}")

            self._set_intents({}, quiet=True)
            time.sleep(3.5)
            idle_again = self._leds()
            self.checker.expect(
                "LEDs return to idle",
                same_color(idle_again[0], (0, 160, 0))
                and all(same_color(color, (0, 0, 0)) for color in idle_again[1:]),
                str(idle_again),
            )
        finally:
            messages, _ = Mqtt.stop(subscriber)

        # Two retained messages are expected; anything else means a relay or
        # pump transition changed the diagnostic posture.
        self.checker.expect(
            "relay transitions do not update error sensors",
            len(messages) == 2,
            f"{len(messages)} messages: {[topic for topic, _ in messages]}",
        )

    # -- heating-switch keep-alive ----------------------------------------

    def check_keepalive(self) -> None:
        """A missed repeat command drops the demand and raises error 301."""
        original_timeout = int(self.safety.get("demand_timeout_s", 0) or 0)
        if original_timeout == 0:
            self.checker.skip("keep-alive timeout", "demand_timeout_s is disabled")
            return

        test_timeout = 10
        try:
            self._put_safety({"demand_timeout_s": test_timeout})
            self._set_intents({}, quiet=True)
            time.sleep(1.5)
            self._set_intents({"heating_loop_1": True}, quiet=True)

            deadline = time.monotonic() + 8.0
            armed: dict[str, Any] = {}
            while time.monotonic() < deadline:
                armed = self._circuit(self._live_state(), "rad1")
                if int(armed.get("demand", 0)) == 1:
                    break
                time.sleep(0.3)
            self.checker.expect(
                "keep-alive arms the circuit demand",
                int(armed.get("demand", 0)) == 1,
                json.dumps(armed, sort_keys=True),
            )

            # Stop refreshing the switch. The raw intent must stay set while
            # the effective demand drops once the timeout expires.
            deadline = time.monotonic() + test_timeout + 8.0
            stale: dict[str, Any] = {}
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = self._live_state()
                stale = self._circuit(state, "rad1")
                if int(stale.get("intended", 0)) == 1 and int(stale.get("demand", 1)) == 0:
                    break
                time.sleep(0.5)
            self.checker.expect(
                "missed keep-alive drops the demand but keeps the intent",
                int(stale.get("intended", 0)) == 1 and int(stale.get("demand", 1)) == 0,
                json.dumps(stale, sort_keys=True),
            )
            code = int(state.get("safety", {}).get("errorCode", -1))
            self.checker.expect("stale demand raises error 301", code == 301, str(code))

            # A fresh ON re-arms the same intent and clears the error.
            self._set_intents({"heating_loop_1": True}, quiet=True)
            deadline = time.monotonic() + 6.0
            refreshed: dict[str, Any] = {}
            state = {}
            while time.monotonic() < deadline:
                state = self._live_state()
                refreshed = self._circuit(state, "rad1")
                if int(refreshed.get("demand", 0)) == 1:
                    break
                time.sleep(0.3)
            self.checker.expect(
                "fresh ON re-arms the demand",
                int(refreshed.get("demand", 0)) == 1,
                json.dumps(refreshed, sort_keys=True),
            )
            self.checker.expect(
                "keep-alive error clears after a refresh",
                int(state.get("safety", {}).get("errorCode", -1)) == 0,
                str(state.get("safety", {}).get("errorCode")),
            )
        finally:
            self._set_intents({}, quiet=True)
            try:
                self._put_safety({"demand_timeout_s": original_timeout}, record=False)
                self.changed_safety.discard("demand_timeout_s")
            except Exception as error:  # noqa: BLE001
                print(f"[ WARN ] could not restore demand_timeout_s: {error}")

    # -- board-owned pump after-run ---------------------------------------

    def check_after_run(self) -> None:
        """The board keeps the pump running briefly after demand ends."""
        start_run = int(self.safety.get("pump_stop_after_heating_s", 0) or 0)
        test_run = 5
        original_window = int(self.safety.get("boiler_min_off_ms", 0) or 0)
        paused_window = False
        try:
            self._put_safety({"pump_stop_after_heating_s": test_run})
            if original_window > 0:
                self._put_safety({"boiler_min_off_ms": 0})
                paused_window = True
            self._set_intents({}, quiet=True)
            time.sleep(1.0)
            self._set_intents({"floor_heating": True}, quiet=True)

            deadline = time.monotonic() + 12.0
            running: dict[str, Any] = {}
            heater: dict[str, Any] = {}
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = self._live_state()
                running = self._circuit(state, "floor")
                heater = state.get("heater", {})
                if (
                    int(running.get("demand", 0)) == 1
                    and int(running.get("phys", 0)) == 1
                    and int(heater.get("phys", 0)) == 1
                ):
                    break
                time.sleep(0.4)
            self.checker.expect(
                "floor demand runs the pump and the heater",
                int(running.get("demand", 0)) == 1
                and int(running.get("phys", 0)) == 1
                and int(heater.get("phys", 0)) == 1,
                f"floor={json.dumps(running, sort_keys=True)} heater={json.dumps(heater, sort_keys=True)}",
            )

            self._set_intents({}, quiet=True)
            # L2 runs every 2 s and L1 applies the relay change on its own
            # tick, so wait for the OFF to propagate before sampling the
            # after-run snapshot instead of assuming a fixed delay.
            deadline = time.monotonic() + 8.0
            after: dict[str, Any] = {}
            heater: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = self._live_state()
                after = self._circuit(state, "floor")
                heater = state.get("heater", {})
                if (
                    int(after.get("demand", 1)) == 0
                    and int(after.get("afterRun", 0)) > 0
                    and int(after.get("phys", 0)) == 1
                    and int(heater.get("phys", 1)) == 0
                ):
                    break
                time.sleep(0.25)
            self.checker.expect(
                "demand ends while the board after-run starts",
                int(after.get("demand", 1)) == 0
                and int(after.get("afterRun", 0)) > 0,
                json.dumps(after, sort_keys=True),
            )
            self.checker.expect(
                "pump keeps running during the board after-run",
                int(after.get("phys", 0)) == 1,
                json.dumps(after, sort_keys=True),
            )
            self.checker.expect(
                "heater stops with the demand (after-run is pump-only)",
                int(heater.get("phys", 1)) == 0,
                json.dumps(heater, sort_keys=True),
            )

            deadline = time.monotonic() + test_run + 10.0
            stopped: dict[str, Any] = after
            while time.monotonic() < deadline:
                stopped = self._circuit(self._live_state(), "floor")
                if int(stopped.get("phys", 1)) == 0:
                    break
                time.sleep(0.5)
            self.checker.expect(
                "after-run stops the pump when it expires",
                int(stopped.get("phys", 1)) == 0
                and int(stopped.get("afterRun", 0)) == 0,
                json.dumps(stopped, sort_keys=True),
            )
        finally:
            self._set_intents({}, quiet=True)
            try:
                self._put_safety({"pump_stop_after_heating_s": start_run}, record=False)
            except Exception as error:  # noqa: BLE001
                print(f"[ WARN ] could not restore pump_stop_after_heating_s: {error}")
            if paused_window:
                try:
                    self._put_safety({"boiler_min_off_ms": original_window}, record=False)
                    self.changed_safety.discard("boiler_min_off_ms")
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore boiler_min_off_ms: {error}")

    # -- mixer opening ceiling --------------------------------------------

    def _floor(self) -> dict[str, Any]:
        live = http_json(self.base, "/api/state")
        return live.get("circuits", {}).get("floor", {})

    def _test_mode(self, active: bool) -> dict[str, Any]:
        path = "/api/test/activate" if active else "/api/test/deactivate"
        return http_json(self.base, path, "POST", {})

    def _wait_mixer(
        self,
        timeout: float,
        predicate: Any,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        floor = self._floor()
        while time.monotonic() < deadline:
            floor = self._floor()
            if predicate(floor):
                break
            time.sleep(0.2)
        return floor

    def check_mixer_cap(self) -> None:
        """The L1 opening ceiling blocks every writer, not just the UI."""
        safety = http_json(self.base, "/api/config/safety")
        cap = int(safety.get("mix_max_open_pct", 0) or 0)
        if not self.checker.expect(
            "mixer opening cap in config (10-100%)", 10 <= cap <= 100, f"{cap}%"
        ):
            self.checker.skip("mixer opening cap enforcement", "invalid config value")
            return

        floor = self._floor()
        live_cap = float(floor.get("mixerMaxPct", -1))
        self.checker.expect(
            "live state reports the mixer cap",
            abs(live_cap - cap) <= 0.5,
            f"{live_cap:g}%",
        )

        # A short travel time keeps the check fast; both constants are live and
        # restored below, so normal control is unaffected afterwards.
        test_cap = 40
        fast_travel_ms = 4000
        original_travel_ms = int(self.hardware.get("travel_time_ms", 0) or 0)
        if original_travel_ms < 1000:
            self.checker.skip(
                "mixer opening cap enforcement", "travel_time_ms unavailable"
            )
            return
        activated = False
        try:
            if not self.test_was_active:
                self._test_mode(True)
                activated = True
            self._put_hardware({"travel_time_ms": fast_travel_ms})
            self._put_safety({"mix_max_open_pct": 100})
            time.sleep(0.5)

            # Park the valve at the end stop so the opening move has a known
            # start position regardless of the preceding checks.
            http_json(
                self.base,
                "/api/test/command",
                "POST",
                {"mixer": "target", "position": 0.0},
            )
            parked = self._wait_mixer(
                15.0,
                lambda f: not f.get("mixerMotion") and float(f.get("mixerPos", 100)) <= 1.0,
            )
            parked_ok = not parked.get("mixerMotion") and float(parked.get("mixerPos", 100)) <= 1.0

            # Start a full-stroke opening move, then lower the ceiling while the
            # motor is running. L1 must stop at the new limit immediately.
            http_json(
                self.base,
                "/api/test/command",
                "POST",
                {"mixer": "target", "position": 1.0},
            )
            opening = self._wait_mixer(
                10.0,
                lambda f: bool(f.get("mixerMotion")) and float(f.get("mixerPos", 0)) >= 15.0,
            )
            started = bool(opening.get("mixerMotion")) and float(opening.get("mixerPos", 0)) >= 15.0
            self.checker.expect(
                "mixer opens above 15% before the cap change",
                parked_ok and started,
                f"parked={parked.get('mixerPos', '?')} opening={opening.get('mixerPos', '?')}",
            )

            self._put_safety({"mix_max_open_pct": test_cap})
            stopped = self._wait_mixer(10.0, lambda f: not f.get("mixerMotion"))
            stopped_pos = float(stopped.get("mixerPos", -1))
            self.checker.expect(
                "L1 stops the mixer at the lowered cap",
                not stopped.get("mixerMotion")
                and test_cap - 5.0 <= stopped_pos <= test_cap + 5.0,
                f"{stopped_pos:.1f}% motion={bool(stopped.get('mixerMotion'))}",
            )
            self.checker.expect(
                "live cap follows the config change",
                abs(float(stopped.get("mixerMaxPct", -1)) - test_cap) <= 0.5,
                f"{stopped.get('mixerMaxPct', '?')}%",
            )

            # Test Mode bypasses L2, so this is the portal's own guard.
            code, _, _ = http_request(
                self.base,
                "/api/test/command",
                "POST",
                {"mixer": "target", "position": 0.8},
            )
            self.checker.expect(
                "portal rejects a target above the cap",
                code == 400,
                f"HTTP {code}",
            )
        finally:
            try:
                self._put_safety({"mix_max_open_pct": cap}, record=False)
                self.changed_safety.discard("mix_max_open_pct")
            except Exception as error:  # noqa: BLE001
                print(f"[ WARN ] could not restore mix_max_open_pct: {error}")
            if original_travel_ms >= 1000:
                try:
                    self._put_hardware({"travel_time_ms": original_travel_ms}, record=False)
                    self.changed_hardware.discard("travel_time_ms")
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore travel_time_ms: {error}")
            if activated:
                try:
                    self._test_mode(False)
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not leave Test Mode: {error}")

    # -- sensor role verification -----------------------------------------

    def check_sensor_roles(self) -> None:
        """Roles are only measurable while the heater and floor pump run."""
        enabled = self.safety.get("sensor_role_check_enabled")
        if enabled is None:
            self.checker.fail(
                "sensor role check configurable", "missing sensor_role_check_enabled"
            )
            return

        original_window = int(self.safety.get("boiler_min_off_ms", 0) or 0)
        activated = False
        paused = False
        try:
            if not enabled:
                self._put_safety({"sensor_role_check_enabled": True})
            if original_window > 0:
                self._put_safety({"boiler_min_off_ms": 0})
                paused = True
            if not self.test_was_active:
                self._test_mode(True)
                activated = True

            for role, value in SAFE_TEMPERATURES.items():
                self._set_temperature(role, value, True, quiet=True)
            # Test Mode drives the heater manually; the HA contract has no
            # heater switch any more. The heater command requires an active
            # pump, so start the floor pump first.
            self._set_test_output("pump_floor", True, quiet=True)
            self._set_test_output("heater", True, quiet=True)

            deadline = time.monotonic() + 15.0
            roles = http_json(self.base, "/api/sensors/roles")
            while time.monotonic() < deadline and not roles.get("measurable"):
                time.sleep(0.5)
                roles = http_json(self.base, "/api/sensors/roles")

            self.checker.expect(
                "roles measurable while heating",
                bool(roles.get("measurable")),
                str(roles.get("summary", "")),
            )
            self.checker.expect(
                "correct sensor order is plausible",
                bool(roles.get("plausible")),
                str(roles.get("summary", "")),
            )

            # Invert boiler and return: the plant cannot produce a warm return
            # from a cold boiler-side sensor, so the roles must look suspect.
            self._set_temperature("boiler", 25.0, True, quiet=True)
            self._set_temperature("mix", 40.0, True, quiet=True)
            self._set_temperature("return", 50.0, True, quiet=True)
            time.sleep(1.5)
            suspect = http_json(self.base, "/api/sensors/roles")
            checks = suspect.get("checks", {})
            self.checker.expect(
                "inverted sensor order reported suspect",
                bool(suspect.get("measurable"))
                and not suspect.get("plausible")
                and not checks.get("boiler_ge_mix", True),
                f"measurable={suspect.get('measurable')} plausible={suspect.get('plausible')}",
            )
        finally:
            try:
                self._set_test_output("heater", False, quiet=True)
                self._set_test_output("pump_floor", False, quiet=True)
                self._set_intents({}, quiet=True)
                for role, value in SAFE_TEMPERATURES.items():
                    self._set_temperature(role, value, True, quiet=True)
            except Exception as error:  # noqa: BLE001
                print(f"[ WARN ] could not restore emulation temperatures: {error}")
            if paused:
                try:
                    self._put_safety({"boiler_min_off_ms": original_window}, record=False)
                    self.changed_safety.discard("boiler_min_off_ms")
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore boiler_min_off_ms: {error}")
            if not enabled:
                try:
                    self._put_safety({"sensor_role_check_enabled": False}, record=False)
                    self.changed_safety.discard("sensor_role_check_enabled")
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not restore sensor_role_check_enabled: {error}")
            if activated:
                try:
                    self._test_mode(False)
                except Exception as error:  # noqa: BLE001
                    print(f"[ WARN ] could not leave Test Mode: {error}")

    # -- optional serial boot check ---------------------------------------

    def check_serial_boot(self) -> None:
        pio = shutil.which("pio")
        if not pio:
            self.checker.skip("serial boot check", "pio not found")
            return
        if not os.path.isdir(self.firmware_dir):
            self.checker.skip("serial boot check", "firmware directory not found")
            return

        if shutil.which("script"):
            command = [
                "timeout",
                "22s",
                "script",
                "-qec",
                f"pio remote device monitor -b 115200 --port {self.args.serial_port}",
                "/dev/null",
            ]
        else:
            command = [
                "timeout",
                "22s",
                pio,
                "remote",
                "device",
                "monitor",
                "-b",
                "115200",
                "--port",
                self.args.serial_port,
            ]

        try:
            monitor = subprocess.Popen(
                command,
                cwd=self.firmware_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            self.checker.skip("serial boot check", str(error))
            return

        time.sleep(4.0)
        try:
            http_json(self.base, "/api/system/reboot", "POST", timeout=5.0)
        except Exception:  # noqa: BLE001 - the reboot may close the connection first
            pass

        try:
            output, _ = monitor.communicate(timeout=30.0)
        except subprocess.TimeoutExpired:
            monitor.kill()
            output, _ = monitor.communicate()
        log = output.replace("\r", "")

        if "pio remote" in log and ("No agents" in log or "not found" in log.lower()):
            self.checker.skip("serial boot check", "remote agent unavailable")
            return

        self.checker.expect("serial boot banner present", "[boot] reset reason:" in log)
        loaded = (
            "[config] loaded saved configuration from NVS" in log
            or "[config] saved configuration format differs" in log
        )
        self.checker.expect("config blob loads on boot", loaded)
        self.checker.expect(
            "no NVS corruption on boot",
            "NVS CRC/format check failed" not in log and "[event] error 501" not in log,
        )
        self.checker.expect(
            "no Preferences read errors on boot",
            "getBytes(): not enough space" not in log
            and "getBytesLength(): nvs_get_blob len fail" not in log,
        )
        crash_markers = ("Guru Meditation", "abort() was called", "Backtrace:")
        self.checker.expect(
            "no crash markers in boot log",
            not any(marker in log for marker in crash_markers),
        )

        # Wait for the controller to come back after the reboot.
        deadline = time.monotonic() + 30.0
        recovered = False
        while time.monotonic() < deadline:
            try:
                status = http_json(self.base, "/api/status", timeout=3.0)
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
                continue
            if status.get("uptime", 0) > 2:
                recovered = True
                self.status = status
                break
            time.sleep(1.0)
        self.checker.expect("controller recovers after reboot", recovered)
        if recovered and self.status.get("mqtt", {}).get("enabled"):
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if self.status.get("mqtt", {}).get("connected"):
                    break
                time.sleep(1.0)
                try:
                    self.status = http_json(self.base, "/api/status", timeout=3.0)
                except Exception:  # noqa: BLE001
                    pass
            self.checker.expect("MQTT reconnects after reboot", bool(self.status["mqtt"]["connected"]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--base-url",
        default=DEFAULT_URL,
        help=f"controller portal URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--broker",
        default=DEFAULT_BROKER,
        help="temporarily point the controller at this MQTT broker for the test",
    )
    parser.add_argument(
        "--broker-port",
        type=int,
        default=DEFAULT_BROKER_PORT if DEFAULT_BROKER else 0,
        help="broker port used with --broker",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="reboot under the remote serial monitor and verify the boot log",
    )
    parser.add_argument(
        "--serial-port",
        default=DEFAULT_SERIAL_PORT,
        help=f"remote serial port for --serial (default: {DEFAULT_SERIAL_PORT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checker = Checker()
    controller = Controller(args, checker)

    if not controller.connect():
        return checker.finish()

    loaded = False
    try:
        controller.prepare()
        controller.run_checks()
        loaded = True
    except KeyboardInterrupt:
        raise
    except Exception as error:  # noqa: BLE001 - keep the summary on any crash
        checker.fail("test run completed", f"{type(error).__name__}: {error}")
    finally:
        controller.restore()

    if args.serial:
        if loaded:
            try:
                controller.check_serial_boot()
            except Exception as error:  # noqa: BLE001
                checker.fail(
                    "serial boot check completed",
                    f"{type(error).__name__}: {error}",
                )
        else:
            checker.skip("serial boot check", "earlier checks did not complete")

    return checker.finish()


if __name__ == "__main__":
    sys.exit(main())
