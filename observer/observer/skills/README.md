# Skills

One folder per skill: `SKILL.md` (front matter + recipe) and optional helper
files, copied into the run's workspace as `skill/` for code in the sandbox.
Recipes use the house model's vocabulary (circuits, roles, locations), never
one house's signal ids, so they can be shared between houses. House-local
skills go into `<OBSERVER_HOME>/skills/` and override these.

Front matter keys: `title`, `description`, `tools` (subset; omitted = all),
`defaults` (e.g. `window: -24h`), `max_steps`.
