"""Tool implementations backing the MCP server.

These are plain async functions over the ESClient. They're defined separately
from the MCP wiring (server.py) so the triage agent can also call them directly
in-process when MCP transport is disabled (useful for tests and single-process
deploys) — the agent's tool schema is generated from the same TOOL_SPECS here,
so the contract is identical either way.
"""
from __future__ import annotations

from typing import Any

from triage.ingest.es_client import ESClient

# JSON-schema tool specs, shared by the MCP server and the Anthropic tool list.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_logs",
        "description": "Full-text search recent diagnostic logs. Use to gather "
                       "the lines around a failure, on a host, or matching a phrase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text match on message"},
                "host": {"type": "string"},
                "subsystem": {"type": "string",
                              "enum": ["gpu", "nvme", "mce", "memory", "thermal",
                                       "network", "kernel", "unknown"]},
                "since_minutes": {"type": "integer", "default": 60},
                "size": {"type": "integer", "default": 25},
            },
        },
    },
    {
        "name": "host_health",
        "description": "Per-subsystem event counts for a host over a window. Use "
                       "to judge whether a host is broadly unhealthy or the "
                       "failure is isolated.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "minutes": {"type": "integer", "default": 60},
            },
            "required": ["host"],
        },
    },
    {
        "name": "similar_incidents",
        "description": "Find historical incidents with a similar signature, to "
                       "spot recurrences and reuse prior resolutions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "signature": {"type": "string"},
                "size": {"type": "integer", "default": 5},
            },
            "required": ["signature"],
        },
    },
    {
        "name": "signature_rate",
        "description": "Bucketed counts for a log signature over a window, to "
                       "confirm whether a failure is accelerating.",
        "input_schema": {
            "type": "object",
            "properties": {
                "signature": {"type": "string"},
                "minutes": {"type": "integer", "default": 60},
                "buckets": {"type": "integer", "default": 6},
            },
            "required": ["signature"],
        },
    },
]


class Tools:
    """Dispatch table mapping tool name -> implementation."""

    def __init__(self, es: ESClient):
        self._es = es

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, f"_{name}", None)
        if fn is None:
            raise ValueError(f"unknown tool: {name}")
        return await fn(**args)

    async def _search_logs(self, query: str | None = None, host: str | None = None,
                           subsystem: str | None = None, since_minutes: int = 60,
                           size: int = 25) -> list[dict[str, Any]]:
        rows = await self._es.search_logs(query=query, host=host, subsystem=subsystem,
                                          since_minutes=since_minutes, size=size)
        # Trim to the fields the model needs — keeps the context window small.
        return [{"ts": r["ts"], "host": r["host"], "subsystem": r["subsystem"],
                 "severity": r["severity"], "message": r["message"],
                 "event_id": r["event_id"]} for r in rows]

    async def _host_health(self, host: str, minutes: int = 60) -> dict[str, int]:
        return await self._es.host_event_summary(host, minutes=minutes)

    async def _similar_incidents(self, signature: str, size: int = 5) -> list[dict[str, Any]]:
        return await self._es.similar_incidents(signature, size=size)

    async def _signature_rate(self, signature: str, minutes: int = 60,
                              buckets: int = 6) -> dict[str, Any]:
        counts = await self._es.signature_rate(signature, minutes=minutes, buckets=buckets)
        return {"signature": signature, "minutes": minutes, "counts": counts}
