# Observer: house understanding, supervision and AI agents — plan

Status: plan (2026-11-01). Implemented so far: data collection, signal
registry and catalog, LLM-assisted descriptions, LLM settings per task,
the reviewed house description, and a first version of the tools, the
sandbox, skills and the agent (phases D–F, see `observer/README.md`). This document describes where the observer goes
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
seen, including unselected ones), the owner's raw input, the owner's current
and previous description, the accepted house description, and data summaries
(value ranges, update rates).

**Two parts (in the house's data directory):**

1. `knowledge.md` — the owner's description: the facts the sensors cannot know
   (building, hydraulics, rooms, comfort targets, history, known issues).
   `knowledge_previous.md` is the version before the last update, edit or
   clear. The raw input is **not stored**.
2. `description.md` — the house description for a human: what the system is,
   every circuit and room, which sensors measure what and where, how the parts
   interact (expected behaviour), and what is uncertain. `model.yaml` is the
   same understanding in machine form: systems, measurement points, relations
   (redundant, order, causal, balance), checks, gaps.

**Cycle:**

1. The owner writes raw input (corrections, missing facts, instructions) in the
   House tab and presses *Update owner's description*. The LLM merges it into
   `knowledge.md`; the input is authoritative on conflicts and the previous
   version is available as a reference (for example "bring back the radiators
   from the previous version"). The actual version can be edited by hand;
   *Clear* empties it and keeps the previous version; *Undo* swaps them.
2. *Create house description* builds a draft from the owner's description and
   the sensors. It can be edited; *Accept and archive* makes it the accepted
   version and keeps it in `house/archive/<time>/` with the owner's description
   and the questions of that time. Analysis and agents read the accepted
   version, or the draft when there is no accepted one.

## 3. Tools

Agents work through tools, never directly on the database or the plant.
Two layers:

* **Core tools** (in the observer, called by the model): house context,
  signal search, data coverage, quick features, series summaries, events,
  `run_python`, the library (list, read, create tool, save skill), finish.
* **Analysis tools** (the library, run in the sandbox by the agent's code):
  generic functions with a manifest (purpose, the kind of data they need,
  typed inputs, output), code (`run(...)`) and tests on synthetic data.
  Examples: resampling with hold horizons, debounce, on-periods, sensor-pair
  comparison, setpoint tracking, first-order-plus-dead-time fits; later heat
  balance, heating-curve fit, valve characteristic, simulator runs
  (`floor_simulator/`), parameter optimisation.

**Data access from code:** the agent decides which data and windows it
needs; its code reads them itself (`obs.data.series(...)`) through the run's
data socket (section 4). Small results go back in tool responses; large ones
stay in the workspace as files.

**Tools and skills created by the LLM:** an agent may write a tool
(`create_tool`) or a skill (`save_skill`) during a run; the *harvest* (a
button on a finished run, its own model setting) proposes them from what a
run did. They go to the house part of the library, unaudited; a tool's tests
run in the sandbox at once. **Audit** is the owner's flag, bound to a hash of
the item (any change voids it); the agent sees the flags and says when a
result depends on an unaudited item. Tools are written against generic
inputs, never one house's signal ids, so they can move to another house with
another sensor set or no controller; promoting a house item to the package
(git) is the owner's decision.

## 4. Sandbox and resources

* **Isolation:** agent-written code runs in **bubblewrap**: unprivileged user
  namespaces (rootless, no daemon), all namespaces unshared (**no network**),
  only `/usr`, its own Python environment, the client library, the tool
  library (read-only) and the run's workspace visible, a cleared environment.
  `podman` (rootless) or `docker` are alternative runtimes.
* **Data access:** the only way out of the sandbox is the run's **data
  socket**, a Unix socket file bound into it. The service behind it runs in
  the observer, validates every request (known signal ids, strict time and
  window formats, size and request limits), reads InfluxDB with a read-only
  token the code never sees, and logs every request. No write access to the
  database, the registry or the controller.
* **Resource limits** per code run: memory and processes (systemd user scope,
  cgroup v2), CPU time and file size (rlimits), wall-clock time; per agent
  run: steps, total time, data requests.
* **Workspace and audit record:** one directory per run with the steps, every
  script, every data request, the files and the report; the conversation is
  saved after each step, so a run **pauses** when the LLM is unavailable
  (quota) and resumes later, possibly with another model.

## 5. Agents and workflows

| Workflow | Character | Uses |
| --- | --- | --- |
| **Supervision** | deterministic, continuous | the checks from the current house model, evaluated by tools every few minutes; alerts over MQTT to Home Assistant |
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
  model.
* Repository layout: shared tools and skills in their own repository
  (observer-library: `tools/<name>/`, `skills/<name>/`, `library.yaml` with
  the library version, changelog, checks); an observer uses it as a library
  folder or as an imported pack (.tgz). House items, history, packs, audit
  flags and test results in `<OBSERVER_HOME>/library/`; the house's
  owner's description (current and previous), house description draft, accepted
  version and model in `<OBSERVER_HOME>/house/`, never in git.

## 7. Phases

| Phase | Deliverable |
| --- | --- |
| A (done) | data collection, registry, catalog, LLM descriptions, per-task model settings |
| B (done) | owner's description updated from raw input (not stored), clear/undo/manual edit; house description built from it and the sensors, draft, accept/archive and model |
| C (first version) | supervision: event, stale, range and stuck checks without LLM, scheduled agent runs, alerts to HA over MQTT discovery; still to come: checks generated from the house model, drift detection, agent investigation triggered by an alert |
| D (first version) | core tools (MCP + function calling); library of analysis tools with manifests, tests and audit flags; seed tools and skills incl. plant identification |
| E (first version) | sandbox: bubblewrap (rootless, no network) with the read-only data socket; limits; workspaces, query logs, audit records |
| F (first version) | agent loop: free questions or skills, own data selection, tool and skill creation, harvest of finished runs, pause/resume on LLM quota; still to come: triggering it from supervision (C) |
| G | optimisation agent with backtesting against recorded data and the simulator; approval flow for parameter pushes |
| H (first version) | sharing library items between houses: separate library repository, versions, export/import as .tgz packs; still to come: a shared remote and pulling updates |

Prerequisites before G: the portal needs a login (it may then approve
parameter pushes). The read-only database token exists (agent tools).
The sandbox uses bubblewrap rather than containers: it needs no daemon and no
root, and the code inside gets data only as files the tools wrote (no
network at all), which is stricter than a container with a database route.
