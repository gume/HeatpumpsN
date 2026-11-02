# Observer changelog

Releases are git tags `observer-v<version>` (the version in `pyproject.toml`).
Each release has a deploy bundle `heatpumpsn-observer-<version>.tar.gz`
(see `deploy/README.md`).

## 0.2.0 - 2026-11-02

First release for deployment.

* Ingest from MQTT (EMS-ESP, the controller, MixerMeter, Zigbee2MQTT, Home
  Assistant Statestream), signal registry and catalog, raw storage in
  InfluxDB.
* Portal: status, signals (bulk edit), LLM proposals, house understanding
  (review, owner knowledge, accepted versions with archive), agent runs,
  library (tools and skills with versions, tests, audit flags, export and
  import), settings (LLM provider and model per task).
* Agent with read-only tools, a bubblewrap sandbox with a read-only data
  socket, harvest of tools and skills from runs, pause and resume on LLM
  quota limits (not available in the container yet).
* Database export and import as one Parquet file, import without
  duplicates; state export and import (registry, house, library, settings).
* Release deployment: Dockerfile, docker compose with InfluxDB, settings
  from the environment, read-only token created at start, optional portal
  password.
