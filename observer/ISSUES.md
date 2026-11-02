# Observer issues

## Missing against the plans (checked 2026-11-02)

Plans: `plans/master_plan.md` (observer role, P4), `plans/observer_node.md`
(§9 responsibilities), `plans/observer_agentic_plan.md` (phases A–H).

| Plan item | State |
| --- | --- |
| Ingest and store (node §9.1, phase A) | done; release deployment ready |
| Reviewed house understanding (phase B) | done; **nothing accepted yet** (a draft waits) |
| **Supervision with alerts to HA** (node §9.4, phase C): checks from the accepted model, drift and anomaly reports, first rule "EMS-ESP offline", alerts as HA entities over MQTT | **missing**: the observer publishes nothing to MQTT yet |
| **Identification from live data** (node §9.2, master P4): transport delay from pulse events, valve characteristic f(pos, T_boiler, T_return), gradient statistics | **missing as an automated job**; tools exist (`fopdt_fit`, `plant_identification` skill), the offline work in `floor_simulator/log_analysis` did it once on HA history |
| **Parameter generation, validation on held-out data, push** (node §9.3, master 9.9, phase G) | **missing**: no backtest against recorded data/simulator, no approval flow, no signed push to the controller's `cmd/mpc/params` (the firmware side exists) |
| Parameter versioning with rollback (node §9.4) | missing (depends on the push) |
| Tool layer, sandbox, agents (phases D–F) | first version done; agents are started by hand, not by supervision; **not available in the release container** (sandbox) |
| Sharing skills between houses (phase H) | separate repository, versions, packs done; no remote, no update pull |
| Portal login (prerequisite for approvals) | HTTP Basic password done; no roles (a read-only user) |
| Old HA history (one year) into the observer | **missing**: a converter from `analysis/heatpumps.duckdb` / HA InfluxDB to the export format (signal mapping) would let `db import` take it |
| Charts of the stored data in the portal (README roadmap) | missing (the agent makes plots; the portal shows none) |
| Offline RL / behavioural cloning (node §9.5, optional) | not started |

Operational gaps of the release: automated backups (a scheduled `db export`
plus state export), disk planning (~1.4 million samples a day: about 1 GB a
year as export files, several GB in InfluxDB; no downsampling or retention
policy yet), monitoring of the observer itself (an HA entity for "observer
alive").

## Open items

Open work on the observer, to be handled later. The roadmap (phases C–H) is in
[`../plans/observer_agentic_plan.md`](../plans/observer_agentic_plan.md);
working agreements in [`HANDOVER.md`](HANDOVER.md) §0.

1. **House understanding quality**: the house task runs on
   `gemini-3.5-flash-lite` and gives a thin model. `gemini-3.8-flash`
   (20 requests/day) would fit the house task (2 requests per draft).
   Generate a draft with the review, check the owner knowledge, accept the
   first version.
2. **Portal has no login**; it listens on `127.0.0.1`. Needed before it may
   approve parameter pushes (prerequisite for plan phase E).
3. **Read-only InfluxDB token** for tools and agents (prerequisite for phase E).
4. **Old HA InfluxDB year**: copy it in once (read-only token) before HA's
   InfluxDB integration is removed.
5. **EMS-ESP** (3.8.4): does not reconnect after a broker restart until
   restarted; bus `txerror` (receives, cannot transmit: check the Tx mode), so
   it only sees what the boiler broadcasts. First supervision rule: EMS-ESP
   offline / heartbeat missing.
6. **Agent: facts it cannot know.** Reports mix old and current behaviour
   (e.g. "encoder did not respond" warnings from before the `stat_t`
   firmware fix, the boot-time ΔT pause) and the unreviewed house
   description misleads (the controller's "boiler" sensor is the
   heat-exchanger secondary, not the boiler flow). Remedies: accept a
   reviewed house description; an operations log in the data directory
   (firmware uploads, settings changes, hardware work) exposed as a tool.
7. **Agent model**: runs use `gemini-3.5-flash-lite`, which ignores the
   library (skills, tools) and writes its own code. Try a stronger model for
   the task *agent* when quota allows, or enforce "read a fitting skill first"
   in the loop.
8. **Sandbox hardening**: add a seccomp filter to bwrap; CPU is limited by
   rlimit only (the user's cgroup has no cpu controller: enable delegation
   with a systemd drop-in if CPU shares matter); `docker`/`podman` runtimes
   are implemented but untested here.
9. **Agent scheduling**: one run at a time, started by hand. Trigger from
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
10. **Git history** still contains the old `observer/registry.yaml` with this
   house's sensor ids and room names (before the data directory existed). Not
   secret; removing it would need a history rewrite.
