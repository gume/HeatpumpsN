"""Agents: read-only tools, a rootless sandbox for generated code, skills and the run loop.

* ``data``    — read-only access to the recorded signals (InfluxDB, read token)
* ``tools``   — the tool set an agent (or an MCP client) may call
* ``sandbox`` — runs agent-written Python without network and with limits
* ``skills``  — recipes in ``observer/skills/<name>/SKILL.md``
* ``runner``  — the agent loop, one workspace and audit record per run
* ``mcp_server`` — the same tools over the Model Context Protocol

See plans/observer_agentic_plan.md. Nothing here can write to the database,
the registry or the plant.
"""
