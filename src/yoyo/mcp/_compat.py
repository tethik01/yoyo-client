"""Server-class compatibility across MCP SDK versions.

`mcp` 1.x exposed the ergonomic server as `mcp.server.fastmcp.FastMCP`; 2.0 renamed it to
`mcp.server.MCPServer`. The decorator and `run("stdio")` surface are the same, so one shim
keeps both working rather than pinning the project to a single SDK generation.
"""

from __future__ import annotations

from typing import Any


def make_server(name: str) -> Any:
    try:  # mcp >= 2.0
        from mcp.server import MCPServer

        return MCPServer(name)
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name)
