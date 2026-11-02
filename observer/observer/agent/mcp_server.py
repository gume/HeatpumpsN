"""The agent tools over the Model Context Protocol (stdio), for any MCP client.

    observer mcp            # e.g. as an MCP server in Claude Code, Claude Desktop or another agent

The server has the same core tools as the built-in agent except ``finish``,
including the library (list, read, create tools and skills; created items are
unaudited). One workspace per server session,
``<OBSERVER_HOME>/runs/<time>-mcp/``, holds the CSV files, plots and sandboxed
scripts; a data socket is open for the session so ``run_python`` code can read
the data. Database access uses the read-only token.
"""
from __future__ import annotations

import os
import time


def serve(cfg: dict) -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    from ..service import Service
    from ..store import MemoryWriter
    from .broker import DataBroker
    from .manager import make_data, make_library, make_sandbox
    from .tools import Toolbox

    service = Service(cfg, MemoryWriter(), "none")      # registry, catalog, house; no ingest
    workspace = os.path.join(cfg["paths"]["runs"], time.strftime("%Y%m%d-%H%M%S-mcp"))
    os.makedirs(workspace, exist_ok=True)
    library = make_library(cfg)
    box = Toolbox(service, make_data(cfg), make_sandbox(cfg, library), workspace, cfg.get("context", ""),
                  library, author=f"MCP session {os.path.basename(workspace)}")
    broker = DataBroker(box, library, os.path.join(workspace, "queries.jsonl")).start()
    box.broker = broker
    server = MCPServer(name="heatpumpsn-observer",
                       instructions="Read-only tools on one house's heating data: start with house_context, "
                                    "then find_signals for exact ids. run_python executes in a sandbox without "
                                    f"network; its working directory is the workspace {workspace}.")
    for tool in box.tools.values():
        if tool.name == "finish":
            continue
        server.add_tool(tool.fn, name=tool.name, description=tool.description,
                        annotations=ToolAnnotations(readOnlyHint=tool.name not in ("run_python", "create_tool",
                                                                                   "save_skill"),
                                                    openWorldHint=False, destructiveHint=False))
    try:
        server.run("stdio")
    finally:
        broker.stop()
