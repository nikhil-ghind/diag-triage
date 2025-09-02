"""Ingest pipeline: raw lines -> parse -> index -> detect -> cluster -> incidents.

This is the synchronous core of the service. The FastAPI layer (app/main.py)
feeds it batches; the agent + router run on the incidents it emits.
"""
from __future__ import annotations

import structlog

from triage.config import Settings
from triage.ingest.detector import Detector
from triage.ingest.es_client import ESClient
from triage.ingest.parser import parse_line
from triage.models import Incident, LogEvent

log = structlog.get_logger(__name__)


class IngestPipeline:
    def __init__(self, settings: Settings, es: ESClient):
        self._s = settings
        self._es = es
        self._detector = Detector(settings, es)

    async def ingest_raw(self, host: str, lines: list[str]) -> tuple[int, list[Incident]]:
        """Parse + index raw lines, returning (#indexed, new incidents)."""
        events = [parse_line(host, ln) for ln in lines if ln.strip()]
        return await self.ingest_events(events)

    async def ingest_events(self, events: list[LogEvent]) -> tuple[int, list[Incident]]:
        if not events:
            return 0, []
        indexed = await self._es.bulk_index_logs(events)

        # Detect against freshly-indexed data; refresh so rate aggs see it.
        try:
            await self._es.es.indices.refresh(index=self._s.es_log_index)
        except Exception:
            pass

        failures = await self._detector.detect(events)
        incidents = self._detector.cluster(failures)
        for inc in incidents:
            await self._es.upsert_incident(inc)
        if incidents:
            log.info("incidents_opened", n=len(incidents),
                     signatures=[i.signature for i in incidents])
        return indexed, incidents
