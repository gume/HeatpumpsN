# System Plan: Multi-Circuit Heating & Floor Mixer Controller — v1

**Purpose of this document:** define the system architecture — actors, responsibilities, signal flow, ownership, and safety invariants. Implementation details (control loop internals, timer mechanics, firmware behavior specifics) live in the sub-plans: the Controller Node Plan, the HA Integration Plan, and the Observer Plan. Section 9 lists open points deferred to those documents.

**Out of scope:** boiler/heater internals, HA automations, controller node implementation details.

---

## 1. Overview

The system controls a central heating distribution module with **three hydraulic circuits** and a **shared boiler/heater call**:

* **Radiator Circuit 1 (unmixed):** circulation pump `pump_rad1`, runs at boiler supply temperature.
* **Radiator Circuit 2 (unmixed):** circulation pump `pump_rad2`, runs at boiler supply temperature.
* **Floor Heating Circuit (mixed):** circulation pump `pump_floor` plus a 3-point motorized mixing valve (`mixer`) blending hot boiler supply with floor return water.
* **Shared heater:** one master boiler/heater relay (`heater`), commanded by the controller from the coordinated circuit demands.

### Actors

* **ESP32 (controller node):** standalone, real-time, safety-critical. Controls all pumps, the mixer valve, and the heater relay. Runs the control loop with local parameters. Never blocks on Home Assistant or the observer. Executes all hardware safety arbitration internally.

* **Home Assistant (hub):** the **single comfort command authority**. Aggregates room sensors, the heating-control integration, and the Wi-Fi mixer encoder; issues the three per-circuit heating-switch commands, floor setpoint, and mode to the controller; receives all controller states. Owns comfort logic. It does **not** command the shared heater or the pump after-run: the controller derives the heater call from the three circuit demands and owns the pump after-run.

* **Heating control (HA integration):** external, internals unspecified. Connected only to Home Assistant. The controller never sees it; its demands arrive indistinguishably from any other HA command.

* **Observer node (PC/RPi):** intermittent. Collects logs (MQTT tap or HA history — observer-side choice, zero firmware impact), performs system identification (delays, gains, valve characteristics, multi-circuit coupling), computes and pushes control loop parameters. **No command path, ever. Absence changes nothing.**

### Topology

```
                         ┌──────────────────┐
                         │  HEATING CONTROL │
                         │  (HA integration)│
                         └────────┬─────────┘
                                  ▼
┌───────────────────── HUB: Home Assistant ──────────────────────┐
│  commands ▼ (heating loops, floor setpoint, mode, encoder)     │
│  states   ▲ (temps, demand/pump/heater states, safety flags)   │
│  comfort logic, UI                                             │
└───────▲───────────────────────────────────────────┬────────────┘
        │ MQTT (commands down / states+logs up)     │ (optional:
        ▼                                           │  HA history)
┌──────────────────┐   loop params (rare)   ┌───────▼──────────┐
│  ESP32           │◀───────────────────────┤  OBSERVER        │
│  CONTROLLER      │───────── states/logs ─▶│  (PC/RPi)        │
│  3 pumps, mixer, │       (MQTT bus tap)   │  intermittent    │
│  heater          │                        └──────────────────┘
└──────────────────┘

```

One publication, two consumers: the controller publishes states/logs to MQTT once; HA consumes them for entities/automations, and the observer taps the same stream (or reads HA history).

---

## 2. Command flow (contracts)

| Signal | From → To | Contract |
| --- | --- | --- |
| Heating-loop commands (`heating_loop_1`, `heating_loop_2`, `floor_heating`) | HA → controller | Independent per-circuit heating demands. Each ON is repeated periodically; **the repetition is that circuit's demand keep-alive**. |
| Heater call | controller internal | Derived by L2 as `floor_heating OR heating_loop_1 OR heating_loop_2` after keep-alive and coordination. There is no HA heater switch. |
| Pump after-run | controller internal | Board-owned run-on after a circuit demand ends; HA does not orchestrate it. |
| Floor forwarding setpoint | HA → controller | Ideal floor forwarding temperature; radiator circuits take none (unmixed) |
| Mixer encoder reading | HA → controller | Forwarded from the Wi-Fi encoder to verify internal dead-reckoning / end-stop homing (possible future: direct I2C or removal) |
| Mode selection (Heat / Summer) | HA → controller | Persisted across reboot |
| Control loop parameters | Observer → controller | Rare, validated, versioned |

**Test override mode** (local web dashboard) enters at the same command intake point as HA commands: same validation, same safety arbitration. It is a **time-limited manual fallback and bench-test entry** (max a few hours, auto-expires, never persists across reboot). It is not a safety bypass. Normal automatic heating without HA is not possible, since room sensors reside in HA.

**Direction summary:** demands flow down (HA → controller → hardware); control parameters flow in sideways (observer → controller); states flow up. The dashboard owns long-term configuration.

---

## 3. Observation flow (contracts)

* Controller publishes to MQTT, in a single publication: temperatures, per-circuit intended/demand/physical states, the derived heater call and its physical state, mixer state, slow log, event log.

* HA consumes: entities for dashboard, automations, heating-control integration.

* Observer consumes: same MQTT topics directly, or HA history — deferred decision, no firmware impact.

* Observer writes only validated control loop parameters.

**Invariant: no observation consumer can command anything.** The observer cannot initiate tests or actuate relays; it may at most suggest test runs for the user to start manually via Test mode.

---

## 4. Failure & timeout semantics (invariants)

Only two mechanisms have control consequences; everything else is monitoring-only.

1. **Per-circuit demand loss:** if HA stops repeating a heating-loop ON command, that circuit's effective demand drops after `demand_timeout_s`. The raw intent stays set, so the next ON re-arms it immediately, and the controller reports error `301` (`ERR_HA_KEEPALIVE_TIMEOUT`) while the demand is stale. *Mechanics and exact timer semantics: Controller Node Plan.*

2. **Pump after-run:** when a circuit demand ends, the controller keeps that circuit's pump running for `pump_stop_after_heating_s`, then stops it. The after-run is per circuit and board-owned; the heater never stays on for an after-run. *Exact allocation: Controller Node Plan.*

3. **MQTT LWT:** "controller offline" visibility in HA only; no control action in either direction.

4. **Observer absence:** never a dependency; last valid parameters persist.

**HA loss during heating is safe by design:** the stale circuit demand drops after `demand_timeout_s`, the derived heater call drops with the last demand, and each pump completes its board-owned after-run before stopping. The condition is reported as `301` and clears when HA refreshes the switch. Test mode covers temporary manual operation until HA is restored.

---

## 5. Safety invariants (system-level)

```
Layer 0 (physical):    hardware overtemp cutout + external pump-cut switch
Layer 1 (firmware):    internal safety arbitration — every output, every tick
Layer 2 (firmware):    staged degradation on demand loss, heater coordination
Layer 3 (validation):  observer parameters validated before adoption
Layer 4 (HA):          comfort/energy logic, visibility — never safety-critical

```

1. **Single path to hardware:** every output (`pump_rad1`, `pump_rad2`, `pump_floor`, `mixer`, `heater`) passes Layer 1 arbitration every tick. **No external actor can weaken it** — parameters of any category tune only within pre-validated bounds.

2. **Single comfort command authority:** Home Assistant owns comfort and issues the three heating-loop switches; the controller owns the derived heater call, pump after-run and safety. HA failure triggers the demand-loss behaviour of §4, by construction.

3. **Observer isolation:** observe and parameterize only; never command.

4. **Floor circuit fails safe:** safe position is **mixer closed** (with end-stop zero-homing when idle); sensor loss drives a defined posture, never "keep last and hope."

5. **Layer 0 exists independently** of any electronics or software.

6. **State transparency:** per-circuit intended, demand and physical states, and the heater's derived command, requested and physical states, are reported separately. `heater_requested ≠ heater_phys` is a normal operating state, not an error, and a controller-initiated heat-off is reported with a reason code.

7. **Heater-without-pump policy:** running the heater with all circulation pumps off is intentionally permitted at Layer 1 (relying on internal boiler protection) and generates a diagnostic warning rather than a safety trip.

*Detailed arbitration rules, postures, and latching semantics: Controller Node Plan (Safety annex).*

---

## 6. Modes & Operating States

| Mode / Operating State | Purpose |
| --- | --- |
| **Heat** | Normal operation across all three circuits |
| **Summer (dry)** | Switchable from HA; all normal heating/pump commands ignored with a warning; persisted across reboot |
| ↳ *Anti-seize exercise* | Controller-initiated, periodic (~monthly) sub-state: brief pump circulation (all 3 pumps) and mixer sweep with heater strictly locked out; the only exception to summer-mode pump lockout |
| **Test override** | Time-limited manual operation and bench testing via web dashboard; same validation path as HA commands |

---

## 7. Parameter ownership

Three disjoint categories; a parameter belongs to exactly one.

| Category | Owner | Examples | Storage |
| --- | --- | --- | --- |
| **Long-term configuration** | Human via web dashboard | Safety/demand timeouts, hardware constants, behavior modes | NVS |
| **Dynamic heating parameters** | Home Assistant | Floor setpoint, Heat/Summer mode | Controller keeps a persisted shadow/fallback |
| **Control loop tuning** | Observer (automated) | Loop coefficients, dead times, coupling deltas, learned weights | NVS, validated against bounds |

**Invariant:** safety-relevant parameters in *any* category are bounded such that no owner can configure an unsafe state (see §5.1).

---

## 8. Development phases

| Phase | Deliverable | Outcome criterion |
| --- | --- | --- |
| P1 | Hardware layer + safety arbitration, manual/test operation only | Safe actuation on test bench; internal dead reckoning matches encoder; safety cutoffs fire |
| P2 | Logging + comms | Complete multi-circuit state records, no data loss over days |
| P3 | Baseline control + HA heating-loop demand interface with per-circuit keep-alive and board-owned after-run | Month of stable operation, safety trips rare |
| P4 | Observer identification from live logs; parameter push live; real heating-control integration as demand source | Measurable improvement in floor loop behavior, including during radiator transients |
| P5 | Predictive/adaptive refinements; optional direct I2C encoder integration or encoder removal | No safety trips under boiler or multi-circuit transients |
| P6 | Optional learned policy, validated offline, behind Layer 1 | Beats P4 controller in simulation before deployment |

*Phase-level test procedures: Controller Node Plan.*

---

## 9. Open points (deferred to sub-plans)

These are known design questions, deliberately not answered here. Each must be resolved in the referenced sub-plan before the relevant phase.

| # | Open point | Resolve in | Phase |
| --- | --- | --- | --- |
| 9.1 | ~~After-heating pump timeout: global or per-pump?~~ Resolved: per-circuit, board-owned (`pump_stop_after_heating_s`). | Controller Node Plan | P3 |
| 9.2 | Timing and trigger conditions for idle mixer closing / end-stop zero-homing | Controller Node Plan | P3 |
| 9.3 | Heater demand for an unsatisfiable floor zone (mixer closed / heat sink unavailable) — HA rule or controller-side coordination posture? | HA Integration Plan + Controller Node Plan | P3 |
| 9.4 | Encoder staleness handling (timestamps, trust policy vs. dead reckoning) | Controller Node Plan | P3 |
| 9.5 | Anti-short-cycle parameter: ownership category and bounds | Controller Node Plan | P1 |
| 9.6 | Effect of shared supply/return sensor failure on radiator circuit operation (if any; floor posture is defined) | Controller Node Plan | P1 |
| 9.7 | ~~Behavior on test-mode expiry mid-heating with HA down~~ Resolved: per-circuit demands go stale after `demand_timeout_s`, the heater drops and the board after-run stops the pumps. | Controller Node Plan | P1 |
| 9.8 | Heater requested/physical divergence: escalation policy | Controller Node Plan | P3 |
| 9.9 | Observer → controller parameter-push channel (MQTT assumed; confirm) | Controller Node Plan + Observer Plan | P4 |
| 9.10 | Encoder I2C migration / removal — trust topology change and its effect on Layer 1 rules | Controller Node Plan | P5 |
