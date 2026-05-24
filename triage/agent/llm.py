"""Anthropic client wrapper with prompt caching.

The triage system prompt + tool definitions are identical on every incident and
dwarf the per-incident payload, so we mark them with `cache_control` and reuse
the cache across calls — turning the bulk of each request into cache reads. An
offline mode returns a deterministic stub so tests and CI run without an API key.
"""
from __future__ import annotations

from typing import Any

from triage.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self._s = settings
        self._client: Any = None
        if not settings.llm_offline and settings.anthropic_api_key:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    @property
    def offline(self) -> bool:
        return self._client is None

    def _cached_system(self, system_prompt: str) -> list[dict[str, Any]]:
        # Single cacheable block; the 5-min TTL is refreshed on each hit so a
        # steady stream of incidents keeps it warm.
        return [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

    def _cached_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Cache the tool definitions too — mark the last tool, which caches the
        # whole tools prefix up to that point.
        if not tools:
            return tools
        out = [dict(t) for t in tools]
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out

    async def create(self, *, system_prompt: str, tools: list[dict[str, Any]],
                     messages: list[dict[str, Any]]) -> dict[str, Any]:
        """One model turn. Returns a normalized dict: {stop_reason, content, usage}."""
        if self.offline:
            return self._offline_turn(messages, tools)

        resp = await self._client.messages.create(
            model=self._s.model,
            max_tokens=self._s.agent_max_tokens,
            system=self._cached_system(system_prompt),
            tools=self._cached_tools(tools),
            messages=messages,
        )
        return {
            "stop_reason": resp.stop_reason,
            "content": [block.model_dump() for block in resp.content],
            "usage": resp.usage.model_dump() if resp.usage else {},
        }

    # --- offline stub -------------------------------------------------------
    def _offline_turn(self, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Deterministic: on the first turn issue one search_logs, then submit.

        Lets the whole agent loop + routing be exercised in tests without a key.
        """
        used_search = any(
            block.get("type") == "tool_result"
            for m in messages if m["role"] == "user"
            for block in (m["content"] if isinstance(m["content"], list) else [])
        )
        if not used_search:
            return {"stop_reason": "tool_use", "usage": {}, "content": [
                {"type": "text", "text": "Investigating the incident."},
                {"type": "tool_use", "id": "stub-1", "name": "search_logs",
                 "input": {"since_minutes": 60, "size": 10}},
            ]}
        return {"stop_reason": "tool_use", "usage": {}, "content": [
            {"type": "tool_use", "id": "stub-2", "name": "submit_triage", "input": {
                "summary": "[offline stub] correlated failures detected.",
                "probable_cause": "offline mode — no model analysis performed.",
                "recommended_action": "Set DIAG_ANTHROPIC_API_KEY to enable triage.",
                "blast_radius": "unknown (offline)",
                "confidence": 0.0,
                "related_incident_ids": [],
                "citations": [],
            }},
        ]}
