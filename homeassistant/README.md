# Home Assistant side

Files for the Home Assistant installation that works with the HeatpumpsN
controller. The folder mirrors HA's own configuration layout, so files can be
copied over as they are.

## Blueprint: heating keep-alive

`blueprints/automation/heatpumpsn/heating_keepalive.yaml`

The controller treats each heating switch (Heating loop 1, Heating loop 2,
Floor heating) as a demand that must be refreshed: without an ON within
`demand_timeout_s` (600 s by default) it drops that circuit's demand and
reports error 301. This blueprint repeats ON while the switch is on and stops
when it is off.

**Install:** copy the file to
`<HA config>/blueprints/automation/heatpumpsn/heating_keepalive.yaml`, then
Developer tools → YAML → *Reload automations* (or restart HA). It appears under
Settings → Automations & scenes → Blueprints.

**Use:** create one automation per switch from the blueprint (*Create
automation*), pick the switch, set the interval. Keep the interval well below
the controller's demand timeout: 120 s for the default 600 s.

**Behaviour:**

* Switch turned on: ON is repeated every interval while it stays on.
* Switch turned off: the repetition stops at once (automation mode `restart`).
* HA restart: if the switch is on, the repetition resumes.
* The switch shows the controller's *effective* demand. After an HA outage
  longer than the timeout the controller has dropped the demand and the switch
  shows off, so the repetition stays stopped until the switch is turned on
  again; the controller re-arms at the next ON.

Requires Home Assistant 2024.10 or newer (`triggers:`/`actions:` syntax).
Checked against HA 2026.10's blueprint schema.
