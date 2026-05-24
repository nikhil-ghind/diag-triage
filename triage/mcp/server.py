"""MCP server exposing the diagnostic-query tools.

Run standalone (`python -m triage.mcp.server`) to serve the tools over stdio so
any MCP client — including the triage agent and external tools like Claude
Desktop — can query the same Elasticsearch-backed diagnostic store. The tool
set and schemas come from triage/mcp/tools.py, so the in-process agent path and
the MCP path are guaranteed identical.
"""
from __future__ import annotations

import asyncio
import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from triage.config import get_settings
from triage.ingest.es_client import ESClient
from triage.mcp.tools import TOOL_SPECS, Tools


def build_server(tools: Tools) -> Server:
    server: Server = Server("diag-triage")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=s["name"], description=s["description"],
                       inputSchema=s["input_schema"])
            for s in TOOL_SPECS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        result = await tools.call(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _amain() -> None:
    settings = get_settings()
    es = ESClient(settings)
    await es.ensure_indices()
    server = build_server(Tools(es))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
