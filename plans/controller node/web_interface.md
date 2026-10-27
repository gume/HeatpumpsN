# Heating Controller Node — Web Portal Documentation

**Version:** 1.0 · **Scope:** ESP32 web portal (also runs on PC via mock server) · **Authority basis:** Master Plan §2, §5, §6, §7

---

## 1. Purpose & Scope

The web portal is the **local human interface** to the heating controller node. It serves two functions:

1. **Long-term configuration** — settings that are human-owned and stored in NVS (§7).
2. **Observation & diagnostics** — live state, logs, sensor health.
3. **Test mode** — the sanctioned manual control entry point (§2, §6).

**Hard rule:** the portal does **not** command the heating process. Setpoints and mode belong to Home Assistant (HA), loop tuning belongs to the observer. The portal displays these read-only. The only manual process intervention is via **Test Mode**, which goes through the same command intake and Layer 1 arbitration as HA commands.

---

## 2. Menu Structure

```
■ Heating Controller                [mode badge: Heat ▏Summer ▏Test]
─────────────────────────────────────
● Dashboard                          live overview (default page)
● Plant                              circuits + heater + mixer temps
● Diagnostics
│   ├─ Event Log
│   ├─ Slow Log
│   ├─ Sensor Health
│   └─ Relay Counters
● Test Mode                          switch, manual control, test sequences
● Board Emulation                    mocked carrier I/O (emulation builds only)
● Settings
│   ├─ Safety & Timeouts
│   ├─ Hardware & Pins
│   ├─ Network & MQTT
│   ├─ Firmware Update
│   └─ System
─────────────────────────────────────
─ Read-only reference ─
● Runtime Parameters 🔒              HA-owned + observer-owned
─────────────────────────────────────
● Connection status (footer, always visible)
```

### Permission scheme (visual, enforced in UI)

| Class | Accent | Examples |
|---|---|---|
| Writable (NVS) | blue accent border | Settings pages |
| Read-only | muted + 🔒 | Plant, Runtime Parameters |
| Guarded | red accent + confirmation | Test Mode |

---

## 3. Page Specifications

### 3.1 Dashboard

One-glance overview. No interaction except navigation.

| Element | Source |
|---|---|
| Mode badge (Heat / Summer / Test) | mode state + authoritative source |
| 3 circuit cards: demand/intent state, physical pump state, after-run countdown | live state |
| Mixer position (commanded %, encoder %) | live state |
| Heater: requested vs. phys (derived from circuit demand), reason code | live state |
| Boiler supply temp, mixer forward/return temps | live state |
| MQTT connected / last disconnect | connection module |
| Wi-Fi RSSI, uptime | system module |
| Active safety flags | safety module |

Layout: status tiles in a responsive grid; tiles with active after-run or safety flags get a highlight state.

### 3.2 Plant

Single page combining all circuits and the heater. Everything read-only.

```
┌──────────────┬──────────────┬───────────────────────────┐
│ Radiator 1   │ Radiator 2   │ Floor (Mixer)             │
│ pump: ● on   │ pump: ○ off  │ pump: ● on                │
│ demand: on   │ demand: off  │ mixer pos: 42%            │
│ after-run: — │ after-run: — │ vs encoder: 41% ✓         │
└──────────────┴──────────────┴───────────────────────────┘
┌──────────────────────────────────────────────────────────┐
│ Mixer temperatures                                       │
│   boiler supply in:   62.4 °C   (mixer hot inlet)        │
│   return in:          38.1 °C   (mixer cold inlet)       │
│   forward (mixed out): 34.2 °C                           │
└──────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────┐
│ Heater (board-managed)                                   │
│   requested: ON   phys: ON ✓   reason: heating           │
│   source: heating loop 1 demand                          │
└──────────────────────────────────────────────────────────┘
```

**Mixer temperatures block (added per request):**
- **Hot inlet** = boiler supply temperature feeding the mixer
- **Cold inlet** = return temperature feeding the mixer
- **Forward** = mixed output temperature (the controlled variable)

Each temperature shows: value, sensor ID, staleness (e.g. "2 s ago"), and health state (ok / stale / error). If a sensor is in error, the tile shows the last valid value greyed out with a warning chip.

**Heater block rules:**
- `heater_requested` and `heater_phys` are always shown side by side; a mismatch is highlighted red (§5.6). The heater has no HA command: it is derived by L2 from the three circuit demands, and Test Mode is the only manual heater source.
- Reason code is shown whenever the controller holds the heater off (§5.6).
- Each circuit card shows its own `demandAge` (time since the last accepted ON) and, after the demand ends, the remaining board-owned `afterRun`. The age turns amber when approaching `demand_timeout_s` and red when the demand has gone stale.

### 3.3 Diagnostics

**3.3.1 Event Log**
- RAM-only circular buffer (96 entries in the current firmware), never written to flash; it resets on reboot.
- Columns: timestamp, severity (info/warn/error/safety), event code, detail string.
- **Safety events are latching** (§6): shown with red badge and "latched" tag until manually acknowledged.
- Filter by severity; download as text file (browser-side blob — no server work).
- Diagnostic posture changes (error code or warning/alarm mask) generate an event, so the same information is mirrored by the HA `system_error_text` sensor.

**3.3.2 Slow Log**
- Long-interval time series sampled every 10 s from the same RAM-only logger (180 samples retained).
- Columns/graph: boiler supply, mixer forward, mixer inlets, return, outdoor (if present), mixer position, pump states.
- Rendered as inline SVG/canvas chart (no external CDN — must work offline on the LAN).
- Retention is the in-RAM buffer size, lost on reboot; no flash budget is consumed. A future flash-backed archive would be a separate, explicit feature.
- Optionally stream the same records to MQTT as JSON lines (`<base>/log/slow`, `<base>/log/event`); see §3.5.3.

**3.3.3 Sensor Health**
- One row per sensor: ID, type, current value, last valid value, last-seen age, error state (ok / stale / disconnected / CRC error / out-of-range).
- Sensor offsets (from Settings) shown inline for context.
- A sensor in error state here correlates with the red tile on Dashboard.

**3.3.4 Relay Counters** *(added per request)*
- One card per switched output: pump_rad1, pump_rad2, pump_floor, heater, mixer-open, mixer-close.
- Values: total switching cycles, switching cycles since last anti-seize/service date, estimated runtime hours (pumps), last operated timestamp.
- Purpose: wear monitoring and justification for the anti-seize exercise schedule (§ Safety settings).
- Counters are NVS-persisted in hardware builds, wear-leveled: a dirty flag is set by cycle or runtime changes and the cache is flushed at most once per hour. Emulation builds keep counters in RAM only and never touch the counter NVS namespace.
- An explicit reset requests an immediate save so a reboot cannot restore old values.
- Writeable action: "reset counters" (per counter, double-confirm, logged as event).

### 3.4 Test Mode

Guarded page. Two zones, both active only while test mode is running.

**Activation block:**
```
Status: INACTIVE                    [Activate Test Mode (2 h)]
Warning: all automation for this node is suspended while active.
Auto-expires after 2 h. Never persisted across reboot.
```
- Confirmation dialog before activation.
- Persistent countdown chip in the header badge while active (visible from every page).
- Deactivation returns all outputs to automation control; a transition event is logged.

**Manual control block** (visible when active):
- Toggles for pump_rad1, pump_rad2, pump_floor, heater.
- Mixer: open / close / stop buttons + target position slider.
- **Every command goes through the command intake + Layer 1 arbitration.** If arbitration refuses (e.g. heater-on without an active pump per coordination rules), the UI shows the refusal with the reason code — the toggle visually snaps back with a toast, it never pretends to have succeeded.
- State feedback uses the same demand/phys pairing as the Plant page, so relay faults are visible during testing.

**Test sequences block:**

| Test | What it does | Related acceptance |
|---|---|---|
| Mixer full sweep | open → close → return to previous position; logs position vs. encoder throughout | P1: dead-reckoning vs. encoder match |
| End-stop homing | drive to end-stop, verify detection, re-home | P1 |
| Anti-seize exercise | run all pumps 30 s sequentially | counter sanity, pump function |
| Heater-pump coordination | attempt illegal & legal heater states, verify arbitration | §5.6 |

- Observer suggestions (§5.3): if the observer has flagged a suggested test, it appears with a "suggested by observer" chip. One-click accept → runs the sequence.

### 3.5 Settings

All pages write to NVS on explicit "Save"; a dirty-state indicator appears on unsaved changes. Bounded numeric inputs use sliders + numeric fields constrained to validated ranges (§5.1).

**3.5.1 Safety & Timeouts**
- Demand-loss timeout (range-limited)
- Board after-run time (`pump_stop_after_heating_s`, applied per circuit)
- Anti-seize schedule (interval, duration)
- Mixer travel timeout (max allowed travel time before fault)

**3.5.2 Hardware & Pins**
- Pin mapping table: pump relays, heater relay, mixer open/close (or 3-point config), sensor buses (1-Wire GPIO, etc.)
- Sensor offset calibration values
- Valve travel time, end-stop configuration (if applicable)
- Warning banner: changes affect Layer 1 arbitration scope; validate on bench.

**3.5.3 Network & MQTT**
- Wi-Fi: SSID, password (masked), fallback AP mode toggle
- MQTT: broker host/IP, port, credentials, base topic. An empty host disables MQTT.
- HA discovery prefix is fixed to `homeassistant`; discovery is published retained with a MAC-derived node ID.
- Availability/LWT uses `<base>/status` with `online`/`offline`.
- "Test MQTT" button queues a non-retained test publication through `/api/mqtt/test`; it reports `202` while the broker is still connecting and `409` when MQTT is disabled.
- Telemetry log streaming: `mqtt_log_enabled` checkbox, plus optional full-topic overrides `mqtt_log_slow_topic` and `mqtt_log_event_topic`. Empty overrides use `<base>/log/slow` and `<base>/log/event`. The logs stay in RAM regardless of this setting.
- Live status chip shows connected broker, connecting state or disabled.

**3.5.4 Firmware Update**
- OTA upload (binary) with progress bar; version + build info display
- LittleFS image upload (optional advanced)
- Warning: do not power off during update; auto-reboot on success.

**3.5.5 System**
- Hostname, NTP server, timezone
- **Summer mode selector** (Heat / Summer): written to NVS as the node's local setting. HA remains primary authority; if HA overrides, the Runtime Parameters page shows which source is currently authoritative. A change here is logged as an event.
- UI theme (light/dark/auto — CSS custom properties)
- Save & reboot, factory reset (double confirmation, logged).

### 3.5a Status LEDs

The main dashboard mirrors the physical animation: LED 0 system/comms, LED 1 boiler/heater/DHW, LED 2 floor circuit/mixer, LEDs 3–4 radiator circuits. Critical alarms flash red; ordinary Wi-Fi/MQTT loss pulses yellow; relay transitions never change the diagnostic error sensors.

### 3.6 Runtime Parameters (read-only)

Single page, two sections, both 🔒-styled:

**HA-owned:**
- Setpoints (per circuit), mode command shadow
- Last-received timestamp per value → makes HA aliveness visible

**Observer-owned:**
- Loop tuning parameters, each with its **version tag** (§2: versioned tuning parameters)
- Observer state/health summary

Refreshing timestamps is the primary "is everything alive" check.

### 3.7 Board Emulation (`HPN_IO_EMULATION` builds only)

This page exists only when the firmware is compiled with the emulated I/O
backend (`esp32dev_sim`); in hardware builds it is not served at all. It is a
development aid, not a plant control.

The page is a direct emulated-carrier control surface. It does not run L1 and
has no heater/pump interlock: every relay can be toggled independently.

**Writable — emulated board and mocked sensor answers** (mapped to `hpn::sim`):

| Element | Range / values | Effect |
|---|---|---|
| Relay buttons (heater, floor, rad1, rad2) | ON/OFF | `sim::set_relay_manual(…)`; overrides that relay |
| Mixer buttons | open / stop / close | `sim::set_mixer_manual(…)` |
| Follow controller | on/off | `sim::set_follow_controller(…)` |
| Realistic current sensors | on/off | `sim::set_realistic_power(…)` |
| Realistic boiler supply | on/off | `sim::set_realistic_boiler(…)`; placeholder sine around 65 °C |
| Clear manual overrides | button | `sim::clear_manual_overrides()` |
| Boiler temperature | −20 … 120 °C + valid toggle | `sim::set_temperature(TempRole::Boiler, …)`; disables the sine |
| Floor return temperature | −20 … 120 °C + valid toggle | `… ReturnFloor …` |
| Mixer output temperature | −20 … 120 °C + valid toggle | `… MixOut …` |
| Floor / Rad 1 / Rad 2 power | 0 … 40 W | `sim::set_power_w(CurrentChannel::Pump*, …)` |
| Mixer power | 0 … 40 W | `sim::set_power_w(CurrentChannel::Mixer, …)` |
| Reset | button | `sim::reset()` — defaults 45/30/35 °C, 10 W pumps, 2 W mixer |

With follow-controller on, L0 drives the emulated relays and mixer; a manual
button overrides only that channel until overrides are cleared or follow mode
is toggled. With realistic current sensors on, pump power follows its relay
(default on-power 10 W) and mixer power follows motion (default moving-power
2 W). The user can set a different on/moving wattage. Setting 0 W while a pump
relay is on simulates a dry run; nonzero power while off simulates a welded
relay — useful to exercise L1 once it exists.

**Read-only — emulated carrier:**

- 74HC595 controller output byte and `OE` state
- effective relay states (heater, rad1, rad2, floor) and manual-override badges
- mixer direction + motion
- selected MUX channel
- model revision

Status LEDs are shown on the main dashboard, not on this page. The page polls
or subscribes to `hpn::sim::snapshot()`; the `revision` field lets it skip
unchanged frames. Changes must not block `T_io`.

---

## 4. Data & Communication Model

### 4.1 Transport

- **One WebSocket connection** for all live data. Server pushes a compact JSON state object on change (throttled, e.g. max 5 Hz); pages subscribe to fields they display.
- **REST endpoints** for config read/write, log access, test sequences, OTA.
- All static assets gzipped, served from LittleFS.

### 4.1.1 Stack decision

**Selected:** `ESP32Async/ESPAsyncWebServer` + `ESP32Async/AsyncTCP` (LGPL-3.0), `ArduinoJson` (MIT), and a vanilla HTML/CSS/JS single-page app stored in LittleFS. One WebSocket at `/ws` carries live state; REST endpoints live under `/api/`.

**ESP-DASH — rejected.** ESP-DASH generates the dashboard from C++ and serves its own pre-built frontend from the library (the v5 source embeds a ~592 KB generated `edp.cpp`). It therefore cannot serve this project's pages, CSS and JS from LittleFS, and its customisation is limited to the commercial Pro edition. The Lite edition is GPL-3.0.

**ReactESP — rejected as the web layer.** ReactESP is an MIT asynchronous event-loop library (an alternative to `loop()`), not a web server or a UI toolkit. It would still need a web server and a frontend, and it would duplicate the existing FreeRTOS `T_comms` scheduler. EMS-ESP's real-time UI is React + ESPAsyncWebServer; ReactESP is not its web framework. ReactESP may be reconsidered only if the RTOS task set is replaced, which is not planned.

**Consequences:** frontend assets live in `firmware/data/` and are uploaded with `pio run -t uploadfs`; no CDN references; gzip precompression. Async server callbacks run outside the real-time tasks, so the portal reads a published state snapshot rather than touching `SystemState` directly (plan 00, real-time core isolation).

### 4.2 State object (WebSocket push, sketch)

```json
{
  "ts": 1234567,
  "mode": {"current": "heat", "source": "ha"},
  "mqtt": {"connected": true, "state": "connected", "broker": "192.168.2.10", "port": 1883, "connectCount": 3, "lastDisconnect": 1720000000, "errors": 0},
  "circuits": {
    "rad1": {"intended": 1, "demand": 1, "cmd": 1, "phys": 1, "demandAge": 12, "afterRun": 0},
    "rad2": {"intended": 0, "demand": 0, "cmd": 0, "phys": 0, "demandAge": 0, "afterRun": 0},
    "floor": {"intended": 1, "demand": 1, "cmd": 1, "phys": 1, "demandAge": 12, "afterRun": 0, "mixerPos": 42, "encoderPos": 41}
  },
  "temps": {
    "boilerSupply": 62.4,
    "mixerHotIn": 62.4,
    "mixerColdIn": 38.1,
    "mixerForward": 34.2,
    "return": 33.8
  },
  "heater": {"automatic": true, "cmd": 1, "requested": 1, "phys": 1, "reason": "heating"},
  "safety": {"flags": [], "latched": []},
  "sensors": [{"id": "28ff...", "val": 62.4, "age": 2, "state": "ok"}],
  "test": {"active": false, "remaining": 0}
}
```

### 4.3 REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/config/:section` | read settings section |
| PUT | `/api/config/:section` | write settings section (validated) |
| POST | `/api/test/activate` / `deactivate` | test mode control |
| POST | `/api/test/command` | manual output command (arbitrated) |
| POST | `/api/test/sequence/:id` | start test sequence |
| GET | `/api/log/events?after=n` | event log tail |
| GET | `/api/log/slow?from=&to=` | slow log range |
| POST | `/api/counters/reset` | relay counter reset |
| POST | `/api/mqtt/test` | queue a test MQTT publication |
| POST | `/api/system/reboot`, `/api/system/factory-reset` | system actions |
| POST | `/api/update` | OTA firmware upload |
| GET | `/api/sim` | emulated carrier + mocked inputs (`HPN_IO_EMULATION` only) |
| POST | `/api/sim/temperature` | mock a temperature answer (emulation only) |
| POST | `/api/sim/power` | set pump/mixer on-power, 0–40 W (emulation only) |
| POST | `/api/sim/relay` | manually override one emulated relay (emulation only) |
| POST | `/api/sim/mixer` | manually drive/stop the emulated mixer (emulation only) |
| POST | `/api/sim/mode` | follow/realistic behaviour switches (emulation only) |
| POST | `/api/sim/overrides/clear` | clear manual relay/mixer overrides (emulation only) |
| POST | `/api/sim/reset` | reset the emulation model (emulation only) |

Config writes are validated server-side against the same range tables the UI uses; out-of-range writes are rejected with the reason.

---

## 5. Frontend

### 5.1 Technology

- Single-page app, vanilla JS (no framework — keeps the gzipped bundle small for LittleFS)
- Sidebar navigation via hash routes (`#dashboard`, `#plant`, `#test`, …); page switching is pure DOM swap, no per-page HTTP fetches
- Live updates via the single WebSocket; each page module registers which state fields it needs
- All assets gzipped, served from LittleFS; works fully offline on the LAN (no CDN references)

### 5.2 Theming (CSS custom properties)

```css
:root {
  --bg:            #1a1d21;
  --panel:         #23272e;
  --text:          #e6e9ec;
  --muted:         #8a919a;

  --accent:        #3b82f6;   /* writable fields            */
  --readonly:      #6b7280;   /* read-only fields (🔒)      */
  --test:          #e5484d;   /* test mode accent           */
  --ok:            #30a46c;
  --warn:          #f5a623;
  --err:           #e5484d;

  --card-border:   #2f343d;
  --dirty-border:  var(--warn);
}
[data-theme="light"] { /* overrides */ }
```

Permission classes applied as utility classes:

```css
.writable  { border: 1px solid var(--accent); }
.readonly  { color: var(--muted); }
.readonly::after { content: " 🔒"; }
.guarded   { border: 1px solid var(--test); }
.dirty     { border: 1px solid var(--dirty-border); }
.state-on  { color: var(--ok); }
.state-off { color: var(--muted); }
.mismatch  { color: var(--err); font-weight: 600; }
```

Users can reskin by overriding the custom properties — no structural changes needed. Dark/light switch lives in Settings → System.

### 5.3 Sidebar Behavior

- Full labels on desktop; collapses to icon bar below 900 px viewport width (boiler-room phone use)
- Header badge always visible: mode, test countdown (when active), MQTT state, Wi-Fi RSSI
- Footer: connection status line (WS state, MQTT state)
- Active route highlighted; unsaved-changes warning when navigating away from a dirty settings page

### 5.4 Page Modules

| Module | Route | Subscribes to |
|---|---|---|
| Dashboard | `#dashboard` | mode, circuits, temps, heater, mqtt, safety |
| Plant | `#plant` | circuits, temps, heater |
| Event Log | `#log/events` | REST poll + push on new entry |
| Slow Log | `#log/slow` | REST fetch + chart render |
| Sensor Health | `#log/sensors` | sensors array |
| Relay Counters | `#log/counters` | counters object |
| Test Mode | `#test` | test object + circuits (for feedback) |
| Settings (×5) | `#settings/*` | REST on load, save via PUT |
| Runtime Parameters | `#runtime` | REST poll every 30 s |

---

## 6. Backend (ESP32)

### 6.1 HTTP/WS Server

- `ESP32Async/ESPAsyncWebServer` + `ESP32Async/AsyncTCP` (selected; see §4.1.1). Fallback if LGPL-3.0 becomes a distribution concern: the ESP-IDF `httpd` component (Apache-2.0).
- Static files from LittleFS with gzip precompression
- WebSocket endpoint `/ws`; REST endpoints under `/api/` (see §4.3)
- JSON via ArduinoJson; state object built from the controller's state module, not by polling hardware directly
- Server work runs in the AsyncTCP task, never in `T_safety`, `T_io` or `T_control`; expose a locked/double-buffered snapshot of `SystemState` to the portal

### 6.2 Integration with the Controller (§5 architecture)

The portal is **just another client of the command intake**:

```
HA (MQTT) ─┐
Test UI  ──┼──▶ command intake ──▶ Layer 1 arbitration ──▶ outputs
           ┘
```

- Test-mode commands enter the same queue with a "test" source tag
- Arbitration refusals are returned to the UI with reason codes (§3.4)
- The portal never writes outputs directly — no code path exists for that
- Safety module has absolute priority; if a safety event fires during test mode, test commands are overridden and the event is latched + shown

### 6.3 Persistence

| Data | Storage | Wear strategy |
|---|---|---|
| Settings | one CRC32-protected NVS blob (`hpncfg`) | written only on explicit Save; older formats discarded, no migration |
| Event log | RAM circular buffer only | never written to flash; optional MQTT stream |
| Relay counters | NVS (`hpnctr`), hardware builds only | CRC32 blob; hourly flush when dirty; immediate after explicit reset |
| Anti-seize schedule | NVS (`hpnanti`), hardware builds only | CRC32 blob; deferred 5-min flush |
| Slow log | RAM circular buffer only | never written to flash; optional MQTT stream |

Factory reset clears settings, counters and the anti-seize schedule; the RAM logs disappear on the next reboot.

### 6.4 OTA

- `/api/update` accepts the firmware binary, streams to the OTA partition, verifies, reboots
- Progress reported over the same WebSocket
- Rollback: ESP32 partition scheme with the previous factory image intact

---

## 7. Mock Server (PC development)

This is separate from the **on-device** I/O emulation
(`HPN_IO_EMULATION`/`hpn::sim`, §3.7). The PC mock server fakes the portal API
for frontend work; the on-device emulation fakes the carrier for L0/L1/L2
development. The same frontend can talk to either.

A Node.js server reproduces the full API so the frontend can be developed and tested without hardware:

```
mock-server/
  server.js        # Express: serves frontend/, implements /api/* and /ws
  state.js         # simulated plant state (temps drift, pumps toggle,
                   #   heater requested/phys with occasional mismatch, sensor errors)
  arbitration.js   # simplified Layer 1 rules so test-mode refusals are realistic
  frontend/        # the actual SPA — identical files later deployed to LittleFS
```

Key behaviors to simulate:
- Temperatures drift realistically (first-order lag toward a target)
- Sensor dropouts (random stale/error states) to exercise the health UI
- A deliberate requested/phys mismatch injection for heater/pumps
- Arbitration refusals (e.g. heater-on with no pump active) to verify the UI shows reason codes
- Test mode countdown timer with auto-expiry

This means the entire UI can be clicked through, themed, and reviewed before any ESP32 endpoint exists.

---

## 8. Acceptance Checklist (portal-specific)

| # | Criterion |
|---|---|
| P-1 | All pages render; navigation works on desktop and narrow screen |
| P-1a | Board Emulation page present in `esp32dev_sim` builds, absent in hardware builds |
| P-2 | Event log shows latching safety events; acknowledgment works |
| P-3 | Slow log chart renders offline (no CDN) |
| P-4 | Sensor health page reflects injected sensor dropouts |
| P-5 | Relay counters increment on simulated switching and persist across mock restart |
| P-6 | Mixer inlets (hot/cold/forward) visible on Plant page with staleness/health states |
| P-7 | Test mode: activation, countdown, arbitration refusal display, auto-expiry |
| P-8 | Settings pages: bounded inputs reject out-of-range values server-side |
| P-9 | Summer mode switch in Settings; Runtime Parameters shows authoritative source |
| P-10 | Runtime Parameters page is strictly read-only; timestamp freshness visible |
| P-11 | Firmware OTA upload completes and reboots (real device) |
| P-12 | Factory reset double-confirmation works |

---

## 9. Open Items

1. **Mixer control type** — 3-point (open/close/stop) wires vs. travel-to-percentage only; affects Test Mode manual controls and Hardware & Pins page.
2. **Slow log retention** — resolved: logs are RAM-only (96 events, 180 slow samples) and never consume flash; optional MQTT streaming provides external retention.
3. ~~After-run timeout granularity — global vs. per-pump~~ Resolved: one `pump_stop_after_heating_s` value applied per circuit, owned by the board; the Safety settings page shows it once.
4. **Summer mode authority** — currently local NVS setting with HA override; confirm HA integration treats this the same way.

---

Next step when you're ready: I build the mock server + frontend skeleton with all 7 pages and placeholder data, matching this spec exactly.
