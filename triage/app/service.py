"""End-to-end orchestration: ingest -> detect/cluster -> triage -> route.

This ties the four subsystems together and owns their lifecycle. The FastAPI
layer holds exactly one TriageService instance.
"""
from __future__ import annotations

import asyncio

import structlog

from triage.agent.llm import LLMClient
from triage.agent.triage_agent import TriageAgent
from triage.config import Settings
from triage.ingest.es_client import ESClient
from triage.ingest.pipeline import IngestPipeline
from triage.mcp.tools import Tools
from triage.models import Alert, Incident, LogEvent, Triage
from triage.routing.router import AlertRouter

log = structlog.get_logger(__name__)


class TriageService:
    def __init__(self, settings: Settings):
        self._s = settings
        self.es = ESClient(settings)
        self.pipeline = IngestPipeline(settings, self.es)
        self.tools = Tools(self.es)
        self.agent = TriageAgent(settings, LLMClient(settings), self.tools)
        self.router = AlertRouter(settings)

    async def startup(self) -> None:
        await self.es.ensure_indices()
        log.info("service_ready", offline_llm=self.agent._llm.offline)

    async def shutdown(self) -> None:
        await self.es.close()

    async def ingest(self, host: str, lines: list[str]) -> dict:
        """Ingest raw lines and triage+route any incidents they open."""
        indexed, incidents = await self.pipeline.ingest_raw(host, lines)
        results = await self._handle_incidents(incidents)
        return {"indexed": indexed, "incidents": len(incidents), "alerts": results}

    async def ingest_events(self, events: list[LogEvent]) -> dict:
        indexed, incidents = await self.pipeline.ingest_events(events)
        results = await self._handle_incidents(incidents)
        return {"indexed": indexed, "incidents": len(incidents), "alerts": results}

    async def _handle_incidents(self, incidents: list[Incident]) -> int:
        """Triage + route each incident concurrently. Returns # alerts sent."""
        if not incidents:
            return 0
        triaged = await asyncio.gather(*(self.agent.triage(i) for i in incidents))
        sent = 0
        for incident, triage in zip(incidents, triaged):
            alerts = await self.router.route(incident, triage)
            sent += len(alerts)
        return sent

    async def triage_one(self, incident: Incident) -> tuple[Triage, list[Alert]]:
        """Triage + route a single (e.g. manually-submitted) incident."""
        triage = await self.agent.triage(incident)
        alerts = await self.router.route(incident, triage)
        return triage, alerts
