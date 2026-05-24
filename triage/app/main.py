"""FastAPI surface for the triage service.

Endpoints:
  GET  /healthz                  liveness + ES reachability
  POST /v1/ingest                raw log lines from a host's agent/forwarder
  POST /v1/ingest/events         pre-parsed LogEvents (e.g. from Filebeat)
  GET  /v1/incidents             recent open incidents
  POST /v1/incidents/{fp}:triage re-run the agent on one incident
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from triage.app.service import TriageService
from triage.config import Settings, get_settings
from triage.models import Incident, LogEvent

log = structlog.get_logger(__name__)


class IngestRequest(BaseModel):
    host: str
    lines: list[str]


class EventsRequest(BaseModel):
    events: list[LogEvent]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    service = TriageService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.startup()
        yield
        await service.shutdown()

    app = FastAPI(title="diag-triage", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    def get_service() -> TriageService:
        return app.state.service

    @app.get("/healthz")
    async def healthz(svc: TriageService = Depends(get_service)):
        return {"status": "ok", "elasticsearch": await svc.es.ping()}

    @app.post("/v1/ingest")
    async def ingest(req: IngestRequest, svc: TriageService = Depends(get_service)):
        return await svc.ingest(req.host, req.lines)

    @app.post("/v1/ingest/events")
    async def ingest_events(req: EventsRequest, svc: TriageService = Depends(get_service)):
        events = [e.finalize() for e in req.events]
        return await svc.ingest_events(events)

    @app.get("/v1/incidents")
    async def incidents(minutes: int = 1440, size: int = 50,
                        svc: TriageService = Depends(get_service)):
        resp = await svc.es.es.search(
            index=settings.es_incident_index,
            query={"range": {"last_seen": {"gte": f"now-{minutes}m"}}},
            sort=[{"severity": "desc"}, {"last_seen": "desc"}],
            size=size,
        )
        return [h["_source"] for h in resp["hits"]["hits"]]

    @app.post("/v1/incidents/{fingerprint}:triage")
    async def retriage(fingerprint: str, svc: TriageService = Depends(get_service)):
        incident = await svc.es.get_incident(fingerprint)
        if incident is None:
            raise HTTPException(404, "incident not found")
        triage, alerts = await svc.triage_one(incident)
        return {"triage": triage.model_dump(mode="json"), "alerts": len(alerts)}

    return app


app = create_app()
