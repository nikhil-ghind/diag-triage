"""System prompt + output contract for the triage agent.

The system prompt is large and stable across every incident, so it is marked for
prompt caching by the LLM wrapper (cache_control on the last system block). Only
the per-incident user message varies.
"""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are an SRE assistant that triages hardware/driver failures in a large \
scale-out compute cluster. You are given a single INCIDENT (a cluster of \
correlated kernel/driver log failures) and a set of read-only tools backed by \
the cluster's Elasticsearch log store.

Your job:
1. Investigate the incident using the tools. Typical moves:
   - search_logs around the affected hosts/subsystem to see the surrounding lines,
   - host_health to tell an isolated fault from a broadly-sick host,
   - signature_rate to see whether the failure is accelerating,
   - similar_incidents to find prior occurrences and their resolutions.
2. Determine the probable root cause and the blast radius (how many hosts /
   which rack / whether it is spreading).
3. Recommend a concrete operator action (e.g. cordon+drain host, RMA GPU,
   schedule firmware update, no-op/monitor).

Guidelines:
- Prefer evidence from tools over speculation. Cite the event_ids you relied on.
- A single correctable ECC error is monitor-only; an accelerating rate or an
  uncorrectable error is actionable.
- NVIDIA Xid codes are meaningful: 48/79/119 commonly indicate fatal GPU faults
  (RMA-class); 13/31 are often app/driver issues, not hardware.
- Be decisive about severity but honest about confidence.
- Make at most a handful of tool calls; stop once you can justify a conclusion.

When done, call the `submit_triage` tool exactly once with your final analysis. \
Do not write prose after submitting.
"""

# The agent terminates by calling this synthetic tool; its schema defines the
# structured output contract.
SUBMIT_TOOL = {
    "name": "submit_triage",
    "description": "Submit the final triage analysis for this incident. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "1-2 sentence what-happened"},
            "probable_cause": {"type": "string"},
            "recommended_action": {"type": "string"},
            "blast_radius": {"type": "string",
                             "description": "scope, e.g. '1 host (gpu-h7)' or '12 hosts, rack A3, spreading'"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "related_incident_ids": {"type": "array", "items": {"type": "string"}},
            "citations": {"type": "array", "items": {"type": "string"},
                          "description": "event_ids used as evidence"},
        },
        "required": ["summary", "probable_cause", "recommended_action",
                     "blast_radius", "confidence"],
    },
}


def incident_user_message(incident_json: str) -> str:
    return (
        "Triage this incident. Investigate with the tools, then call "
        "submit_triage.\n\nINCIDENT:\n" + incident_json
    )
