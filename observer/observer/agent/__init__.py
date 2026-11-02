"""Agents: read-only tools, a rootless sandbox for generated code, skills and the run loop.

* ``data``    — read-only access to the recorded signals (InfluxDB, read token)
* ``tools``   — the core tools the model calls (or an MCP client)
* ``sandbox`` — runs agent-written Python without network and with limits
* ``broker``  — the run's data socket: how sandboxed code reads the data
* ``library`` — analysis tools (code + tests) and skills (recipes), with audit
  flags; package items in ``observer/library``, house items in the data directory
* ``runner``  — the agent loop, one workspace and audit record per run
* ``manager`` — background runs for the portal
* ``mcp_server`` — the core tools over the Model Context Protocol

See plans/observer_agentic_plan.md. Nothing here can write to the database,
the registry or the plant; agents can only add unaudited items to the house
part of the library.
"""
