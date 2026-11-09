# Observer issues

## Missing against the plans (checked 2026-11-04, release 0.6.0)

Plans: `plans/master_plan.md` (observer role, P4), `plans/observer_node.md`
(§9 responsibilities), `plans/observer_agentic_plan.md` (phases A–H).

| Plan item | State |
| --- | --- |
| Ingest and store (node §9.1, phase A) | done; the collector in its own container and image (0.5.0, 0.6.0); one install folder with `update.sh` / `cleanup.sh` (0.6.0) |
| Reviewed house understanding (phase B) | done; **nothing accepted yet** (a draft waits); a stronger model can be chosen for one draft on the House tab (0.6.0) |
| **Supervision with alerts to HA** (node §9.4, phase C): checks from the accepted model, drift and anomaly reports, first rule "EMS-ESP offline", alerts as HA entities over MQTT | **first version** (0.4.0): event, stale, range, stuck and scheduled agent checks, alerts to HA; drift alert for the mixer model (0.5.0); still missing: checks derived from the accepted house model, drift detection beyond the mixer, an agent investigation triggered by an alert |
| **Identification from live data** (node §9.2, master P4): transport delay from pulse events, valve characteristic f(pos, T_boiler, T_return), gradient statistics | **first version** (0.5.0): the controller's mixer model (law, transport delay, sensor lags, per pump scenario) re-identified daily or on the Parameters tab; the slow log (exact model inputs) is stored, until enough of it exists the published values are used; still missing: **gradient statistics** (safe gradient limits, pulse size caps) |
| **Parameter generation, validation on held-out data, push** (node §9.3, master 9.9, phase G) | **first version** (0.5.0): proposal only when clearly better on held-out data, closed-loop backtest with the controller's MPC core, approval in the portal, HMAC-signed push to `cmd/mpc/params` with the controller's acknowledgement; **sending is off** (`parameters.push_enabled`) and **not yet tried against the real controller**; no optimisation agent that proposes parameter sets or boiler settings itself |
| Parameter versioning with rollback (node §9.4) | done (0.5.0): every sent set keeps the one it replaced for a roll back |
| Tool layer, sandbox, agents (phases D–F) | first version done; simulator for the agent (`obs.sim`, 0.5.0); agents are started by hand or by a scheduled check, not by an alert; in the release container with `docker-compose.sandbox.yml` (0.3.1) |
| Sharing skills between houses (phase H) | separate repository, versions, packs done; no remote, no update pull |
| Portal login (prerequisite for approvals) | HTTP Basic password done; no roles (a read-only user) |
| Old HA history (one year) into the observer | **missing**: a converter from `analysis/heatpumps.duckdb` / HA InfluxDB to the export format (signal mapping) would let `db import` take it |
| Charts of the stored data in the portal (README roadmap) | missing (the agent makes plots; the portal shows none) |
| Offline RL / behavioural cloning (node §9.5, optional) | not started |

Operational gaps of the release: automated backups (a scheduled `db export`
plus state export), disk planning (~1.4 million samples a day: about 1 GB a
year as export files, several GB in InfluxDB; no downsampling or retention
policy yet), monitoring of the observer itself (an HA entity for "observer
alive"; the portal already alarms when the collector is silent). A log file
exists since 0.6.0 (`data/logs/observer.log`, Settings → Log).

## Open items

Open work on the observer, to be handled later. The roadmap (phases C–H) is in
[`../plans/observer_agentic_plan.md`](../plans/observer_agentic_plan.md);
working agreements in [`HANDOVER.md`](HANDOVER.md) §0.

1. **House understanding quality**: the house task runs on
   `gemini-3.5-flash-lite` and gives a thin model. `gemini-3.8-flash`
   (20 requests/day) would fit the house task (2 requests per draft).
   Generate a draft with the review, check the owner knowledge, accept the
   first version.
2. **Portal roles**: the portal has an HTTP Basic password
   (`OBSERVER_PORTAL_PASSWORD`); a read-only user (view without approving
   parameter pushes) is missing.
3. **First parameter push**: set `parameters.push_enabled` and
   `OBSERVER_CONTROLLER_SECRET`, approve one proposal and check the
   controller's acknowledgement and a roll back on the real plant (S2 upload
   rules apply if the firmware side needs a change). Status 2026-11-09: the
   first re-identification on the exact slow log (39.6 h) is in
   [`docs/mixer-identification-2026-11-09.md`](docs/mixer-identification-2026-11-09.md);
   its proposal `edd054c886` has a **not recommended** closed-loop backtest
   (twice the motor starts/reversals), so it was not approved and nothing was
   sent. The current parameters stay until a proposal is recommended or the
   owner accepts one explicitly. The controller side is ready: the S2 got the
   2026-11-09 firmware and portal by OTA (the retry-block banner, the unblock
   API and the `retry*` status fields are live); still nothing was pushed.
4. **Boiler setpoint history**: `ems-esp/boiler_data#heatingtemp` was
   `store: false` in the data directory's registry, so the 70 -> 60 °C change
   on 2026-11-08 has no database history. It is set to `store: true` since
   2026-11-09 15:58; re-identify after several days at 60 °C.
5. **Old HA InfluxDB year**: copy it in once (read-only token) before HA's
   InfluxDB integration is removed.
6. **EMS-ESP** (3.8.4): does not reconnect after a broker restart until
   restarted; bus `txerror` (receives, cannot transmit: check the Tx mode), so
   it only sees what the boiler broadcasts. First supervision rule: EMS-ESP
   offline / heartbeat missing.
7. **Agent: facts it cannot know.** Reports mix old and current behaviour
   (e.g. "encoder did not respond" warnings from before the `stat_t`
   firmware fix, the boot-time ΔT pause) and the unreviewed house
   description misleads (the controller's "boiler" sensor is the
   heat-exchanger secondary, not the boiler flow). Remedies: accept a
   reviewed house description; an operations log in the data directory
   (firmware uploads, settings changes, hardware work) exposed as a tool.
8. **Agent model**: runs use `gemini-3.5-flash-lite`, which ignores the
   library (skills, tools) and writes its own code. Try a stronger model for
   the task *agent* when quota allows, or enforce "read a fitting skill first"
   in the loop.
9. **Sandbox hardening**: add a seccomp filter to bwrap; CPU is limited by
   rlimit only (the user's cgroup has no cpu controller: enable delegation
   with a systemd drop-in if CPU shares matter); `docker`/`podman` runtimes
   are implemented but untested here.
10. **Agent scheduling**: one run at a time, started by hand. Trigger from
   supervision (phase C) and scheduled skills are open; old runs are never
   deleted (decide a retention). A paused run is only visible in the portal:
   notify the owner (HA notification over MQTT) once alerts exist.
11. **Library**: bringing a house item into the observer-library repository
    is manual (export, unpack, raise the version, changelog, check, commit);
    the repository has no remote yet. Tools have no dependency declaration
    beyond `requires` (the sandbox has numpy, pandas, scipy, matplotlib); a
    tool calls another only through `obs.tools` at run time.
12. **Room analysis needs data**: free-cooling fits on ~15 h are weak
    (r2 < 0.3, daytime gains and the ACs dominate); several nights are
    needed. Four BLE room sensors (David, Sport, Bathroom TF, AA) report too
    rarely for 5-minute grids (12-45 samples in 15 h): fix the reception or
    analyse on a coarser grid. Heating-related room skills (heat-up,
    emitter sizing) wait for the radiator circuits to run.
13. **Git history** still contains the old `observer/registry.yaml` with this
   house's sensor ids and room names (before the data directory existed). Not
   secret; removing it would need a history rewrite.