"""The agentic triage loop.

Given an Incident, the agent runs a tool-use loop against the diagnostic tools
(over MCP, or in-process via the same Tools dispatch) and terminates by calling
the synthetic `submit_triage` tool, whose arguments become the structured
Triage result. The loop is bounded by `agent_max_tool_turns`.
"""
from __future__ import annotations

import json

import structlog

from triage.agent.llm import LLMClient
from triage.agent.prompts import SUBMIT_TOOL, SYSTEM_PROMPT, incident_user_message
from triage.config import Settings
from triage.mcp.tools import TOOL_SPECS, Tools
from triage.models import Incident, Triage

log = structlog.get_logger(__name__)


class TriageAgent:
    def __init__(self, settings: Settings, llm: LLMClient, tools: Tools):
        self._s = settings
        self._llm = llm
        self._tools = tools
        # The model sees the read-only query tools plus the terminal submit tool.
        self._tool_defs = TOOL_SPECS + [SUBMIT_TOOL]

    async def triage(self, incident: Incident) -> Triage:
        messages: list[dict] = [{
            "role": "user",
            "content": incident_user_message(incident.model_dump_json(indent=2)),
        }]
        tool_calls = 0

        for _turn in range(self._s.agent_max_tool_turns):
            resp = await self._llm.create(
                system_prompt=SYSTEM_PROMPT, tools=self._tool_defs, messages=messages)
            content = resp["content"]
            messages.append({"role": "assistant", "content": content})

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                break  # model stopped without tools — fall through to fallback

            # Did it submit? That terminates the loop.
            submit = next((t for t in tool_uses if t["name"] == "submit_triage"), None)
            if submit:
                return self._to_triage(incident, submit["input"], tool_calls,
                                       resp.get("usage", {}))

            # Otherwise execute the query tools and feed results back.
            results = []
            for tu in tool_uses:
                tool_calls += 1
                try:
                    out = await self._tools.call(tu["name"], tu.get("input", {}))
                    payload = json.dumps(out, default=str)
                except Exception as exc:  # surface tool errors to the model
                    payload = json.dumps({"error": str(exc)})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": payload,
                })
            messages.append({"role": "user", "content": results})

        # Loop exhausted or model gave up without submitting.
        return self._fallback(incident, tool_calls)

    def _to_triage(self, incident: Incident, data: dict, tool_calls: int,
                   usage: dict) -> Triage:
        log.info("triage_complete", incident=incident.incident_id,
                 tool_calls=tool_calls,
                 cache_read=usage.get("cache_read_input_tokens"),
                 cache_write=usage.get("cache_creation_input_tokens"))
        return Triage(
            incident_id=incident.incident_id,
            summary=data.get("summary", ""),
            probable_cause=data.get("probable_cause", ""),
            recommended_action=data.get("recommended_action", ""),
            blast_radius=data.get("blast_radius", ""),
            confidence=float(data.get("confidence", 0.0)),
            related_incident_ids=data.get("related_incident_ids", []),
            citations=data.get("citations", []),
            model=self._s.model,
            tool_calls=tool_calls,
        )

    def _fallback(self, incident: Incident, tool_calls: int) -> Triage:
        return Triage(
            incident_id=incident.incident_id,
            summary=f"{incident.count} '{incident.subsystem.value}' failures on "
                    f"{len(incident.hosts)} host(s) (agent did not converge).",
            probable_cause="Inconclusive — see raw logs.",
            recommended_action="Manual review required.",
            blast_radius=f"{len(incident.hosts)} host(s)",
            confidence=0.0,
            model=self._s.model,
            tool_calls=tool_calls,
        )
