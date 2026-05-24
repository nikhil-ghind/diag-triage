"""Agent loop drives tool calls and terminates on submit_triage.

Uses the LLM offline stub (no API key) and a fake Tools dispatch so the loop is
exercised deterministically: turn 1 issues search_logs, turn 2 submits.
"""
from triage.agent.llm import LLMClient
from triage.agent.triage_agent import TriageAgent
from triage.config import Settings
from triage.models import Incident, Severity, Subsystem


class FakeTools:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return [{"ts": "now", "host": "n1", "subsystem": "gpu",
                 "severity": "critical", "message": "Xid 79", "event_id": "ev1"}]


def _incident():
    return Incident(incident_id="i1", signature="nvrm: xid <n>",
                    subsystem=Subsystem.gpu, severity=Severity.critical,
                    count=3, hosts=["n1"], fingerprint="fp1")


async def test_agent_loop_calls_tool_then_submits():
    s = Settings(llm_offline=True)
    tools = FakeTools()
    agent = TriageAgent(s, LLMClient(s), tools)
    triage = await agent.triage(_incident())

    # Offline stub calls search_logs once, then submits.
    assert ("search_logs", {"since_minutes": 60, "size": 10}) in tools.calls
    assert triage.incident_id == "i1"
    assert triage.tool_calls == 1
    assert "offline stub" in triage.summary.lower()


async def test_agent_offline_is_low_confidence():
    s = Settings(llm_offline=True)
    agent = TriageAgent(s, LLMClient(s), FakeTools())
    triage = await agent.triage(_incident())
    assert triage.confidence == 0.0
