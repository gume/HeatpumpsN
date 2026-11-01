# Observer: house understanding, supervision and AI agents — plan

Status: plan (2026-11-01). Implemented so far: data collection, signal
registry and catalog, LLM-assisted descriptions, LLM settings per task
(see `observer/README.md`). This document describes where the observer goes
next and in which order.

## 1. Goals

1. **Observe and optimise:** understand the heating system from its data and
   improve it (controller parameters, boiler settings, schedules).
2. **Find anomalies and alert:** faulty or stuck sensors, devices offline,
   short-cycling, circuits that do not deliver heat, unexpected behaviour.
3. **Stay understandable and correctable by a human:** every conclusion rests
   on a description of the house that the owner reviews and can extend.
4. **Be reusable:** tools, recipes and skills can be shared between houses;
   house-specific knowledge stays with the house.

Invariant from the system plan (`plans/master_plan.md` §1, §5.3): the observer
never commands the plant. Its only outputs are reports, alerts and **proposed**
parameter sets that are applied through the controller's signed parameter
channel after the owner approves them.

## 2. House understanding: the reviewed description

The base of everything else is a description of the house that a human can
read, check and extend.

**Inputs:** the house context (configuration), the signal registry (names,
units, locations, circuits, roles, descriptions), the catalog (all signals
seen, including unselected ones), the owner's review notes, the previous
audited description, and data summaries (value ranges, update rates).

**Output (proposed by the LLM, reviewed by the owner):**

* `description.md` — prose for a human: what the system is, every circuit and
  room, which sensors measure what and where, how the parts interact
  (expected behaviour), and what is uncertain;
* `model.yaml` — the same understanding in machine form: systems, measurement
  points, relations (redundant, order, causal, balance), checks, gaps;
* **questions** — what the model is unsure about, for the owner to answer;
* **recommendations** — sensors that would improve analysis, and signals that
  already exist in the catalog but are not used (like the outdoor temperature
  was).

**Review loop:**

1. The owner reads the proposed description, edits it directly if needed, and
   writes **review notes** (corrections, missing facts, answers to the
   questions). The notes are always open and kept in git (`observer/house/notes.md`).
2. Regenerating uses the notes and the previous audited version, so knowledge
   accumulates instead of being lost.
3. **Accepting** makes the proposal the audited version and archives it
   (`observer/house/archive/<timestamp>/`, with the notes at that moment).
   Analysis and agents always read the latest audited version.

## 3. Tools

Agents work through tools, never directly on the database or the plant.

| Kind | Examples |
| --- | --- |
| Data access (read-only) | time series of signals by id, tag (location, circuit, role) or house-model relation; resampled at query time; event log |
| Features | delta-T, heat-up times, cycle counts and lengths, duty cycles, correlations, response delays, stuck-value and staleness detection |
| Calculation | steady-state heat balance, heating curve fit, degree days, valve characteristic fit |
| Simulation | the floor simulator's plant model (`floor_simulator/`), closed loop with the firmware MPC core (`fw_mpc`), what-if runs |
| Solvers | parameter fitting (least squares), optimisation of MPC parameter sets against recorded data |

**Interface:** tools are exposed as MCP servers (Model Context Protocol), so any
MCP-capable agent can use them; a plain HTTP/JSON or Python API wraps the same
functions for deterministic jobs. Small results go back in the response; large
ones (data frames, plots) are written to the agent's workspace as files
(Parquet/CSV/PNG) and returned as paths.

**Tool creation by the LLM:** an agent may write a new tool (code + schema +
tests) when no existing one fits. A new tool runs only in the sandbox, must
pass its tests on recorded data, and is promoted to the shared tool set only
after review (by the owner or a stronger reviewing model, then the owner).

## 4. Sandbox and resources

* **Isolation:** tools and agent-generated code run in **rootless** containers
  (Podman rootless or Docker rootless; gVisor or a microVM such as Firecracker
  are stronger options if needed). No root inside or outside, no privileged
  mode, read-only root filesystem, dropped capabilities, seccomp profile.
* **Data access:** a read-only InfluxDB token scoped to the observer's bucket;
  no write access to the database, the registry or the controller. The
  network is limited to the database (and the LLM endpoint for the agent
  process itself).
* **Resource limits** per workspace and per run: CPU, memory, disk quota,
  wall-clock time, number of processes; results and logs kept for audit.
* **Workspace:** one directory per run (inputs, tool outputs, the agent's notes
  and the final report), archived with the run's audit record.

## 5. Agents and workflows

| Workflow | Character | Uses |
| --- | --- | --- |
| **Supervision** | deterministic, continuous | the checks from the audited house model, evaluated by tools every few minutes; alerts over MQTT to Home Assistant |
| **Anomaly investigation** | agentic, on trigger | when a check fires or something looks unusual: gathers data with tools, tests hypotheses against the house model, reports the likely cause and what to check |
| **Optimisation** | agentic, scheduled or on request | identifies plant parameters (delays, gains), proposes controller parameter sets or boiler settings, **backtests** them against recorded data and the simulator, and reports the expected gain with evidence |
| **House understanding** | LLM, on request | §2 |

Every agentic run produces a report: question, data used, tools called,
findings, confidence, proposal. Proposals that change something (parameters,
settings) wait for the owner's approval in the portal; approved parameter sets
go out through the controller's signed channel and are logged.

**Models per role:** the owner chooses the provider and model per task
(already implemented for descriptions and the house model): a cheap, fast
model for routine work, a strong reasoning model for investigation and
optimisation.

## 6. Skills and recipes

* A **recipe** is a written procedure for a recurring task ("check the floor
  circuit after a heating start", "fit the heating curve", "detect a stuck
  sensor"), in Markdown, naming the tools it uses and the house-model
  relations it needs.
* A **skill** packages one or more recipes with their tools and tests.
* Recipes and skills are written against the house model's vocabulary
  (circuits, roles, relations), not against one house's signal ids, so they
  can be **shared between houses**; each house maps them through its own
  audited model.
* Repository layout (planned): `observer/skills/<name>/` (recipe, tools,
  tests), `observer/house/` (house-specific description, model, notes,
  archive).

## 7. Phases

| Phase | Deliverable |
| --- | --- |
| A (done) | data collection, registry, catalog, LLM descriptions, per-task model settings |
| B (now) | reviewed house description with notes, questions, recommendations and archive |
| C | deterministic supervision from the audited model's checks; alerts to HA |
| D | read-only tool layer (data, features, calculation) as MCP + Python API; first recipes |
| E | sandbox (rootless containers, resource limits), workspaces, audit records |
| F | anomaly-investigation agent |
| G | optimisation agent with backtesting against recorded data and the simulator; approval flow for parameter pushes |
| H | LLM-written tools with tests and review; shareable skills |

Prerequisites before E: the portal needs a login (it may then approve
parameter pushes), and the read-only database token.
