# Observer issues

Open work on the observer, to be handled later. The roadmap (phases C–H) is in
[`../plans/observer_agentic_plan.md`](../plans/observer_agentic_plan.md);
working agreements in [`HANDOVER.md`](HANDOVER.md) §0.

1. **House description quality**: the house task runs on
   `gemini-3.5-flash-lite` and gives a thin model. Switch the house task to a
   stronger model (`gemini-2.5-pro` on the proxy) in Settings, regenerate,
   review and accept the first version (a proposal is waiting).
2. **Portal has no login**; it listens on `127.0.0.1`. Needed before it may
   approve parameter pushes (prerequisite for plan phase E).
3. **Read-only InfluxDB token** for tools and agents (prerequisite for phase E).
4. **Old HA InfluxDB year**: copy it in once (read-only token) before HA's
   InfluxDB integration is removed.
5. **EMS-ESP** (3.8.4): does not reconnect after a broker restart until
   restarted; bus `txerror` (receives, cannot transmit: check the Tx mode), so
   it only sees what the boiler broadcasts. First supervision rule: EMS-ESP
   offline / heartbeat missing.
6. **Git history** still contains the old `observer/registry.yaml` with this
   house's sensor ids and room names (before the data directory existed). Not
   secret; removing it would need a history rewrite.
