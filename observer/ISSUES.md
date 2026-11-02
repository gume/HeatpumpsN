# Observer issues

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
7. **Agent model**: runs use `gemini-3.5-flash-lite`; compare with
   `gemini-2.5-pro` on the same skill and keep the better one for the task.
8. **Sandbox hardening**: add a seccomp filter to bwrap; CPU is limited by
   rlimit only (the user's cgroup has no cpu controller: enable delegation
   with a systemd drop-in if CPU shares matter); `docker`/`podman` runtimes
   are implemented but untested here.
9. **Agent scheduling**: one run at a time, started by hand. Trigger from
   supervision (phase C) and scheduled skills are open; old runs are never
   deleted (decide a retention).
10. **Git history** still contains the old `observer/registry.yaml` with this
   house's sensor ids and room names (before the data directory existed). Not
   secret; removing it would need a history rewrite.
