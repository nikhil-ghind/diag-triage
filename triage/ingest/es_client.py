"""Async Elasticsearch access: index templates, ILM, bulk indexing, queries.

Index strategy:
  * `diag-logs`     — append-only event store, time-based, ILM hot->delete.
  * `diag-incidents`— current incident state, upserted by fingerprint.

The MCP tools (triage/mcp/tools.py) read through this same client so the agent
queries exactly the data the pipeline ingested.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from elasticsearch import AsyncElasticsearch, NotFoundError
from elasticsearch.helpers import async_bulk

from triage.config import Settings
from triage.models import Incident, LogEvent


LOG_MAPPING: dict[str, Any] = {
    "properties": {
        "ts": {"type": "date"},
        "host": {"type": "keyword"},
        "subsystem": {"type": "keyword"},
        "severity": {"type": "keyword"},
        "message": {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 2048}}},
        "signature": {"type": "keyword"},
        "event_id": {"type": "keyword"},
        "fields": {"type": "object", "enabled": True},
    }
}

INCIDENT_MAPPING: dict[str, Any] = {
    "properties": {
        "incident_id": {"type": "keyword"},
        "signature": {"type": "keyword"},
        "subsystem": {"type": "keyword"},
        "severity": {"type": "keyword"},
        "opened_at": {"type": "date"},
        "last_seen": {"type": "date"},
        "count": {"type": "integer"},
        "hosts": {"type": "keyword"},
        "fingerprint": {"type": "keyword"},
        "sample_messages": {"type": "text"},
    }
}


class ESClient:
    def __init__(self, settings: Settings):
        self._s = settings
        kwargs: dict[str, Any] = {"hosts": settings.es_hosts}
        if settings.es_api_key:
            kwargs["api_key"] = settings.es_api_key
        self.es = AsyncElasticsearch(**kwargs)

    async def close(self) -> None:
        await self.es.close()

    async def ping(self) -> bool:
        try:
            return await self.es.ping()
        except Exception:
            return False

    # --- schema setup -------------------------------------------------------
    async def ensure_indices(self) -> None:
        """Create ILM policy + indices if absent. Idempotent."""
        policy = {
            "policy": {
                "phases": {
                    "hot": {"actions": {"rollover": {"max_age": f"{self._s.es_ilm_hot_days}d",
                                                     "max_primary_shard_size": "30gb"}}},
                    "delete": {"min_age": f"{self._s.es_ilm_delete_days}d",
                               "actions": {"delete": {}}},
                }
            }
        }
        try:
            await self.es.ilm.put_lifecycle(name="diag-logs-ilm", body=policy)
        except Exception:
            pass  # ILM unavailable (e.g. single-node OSS) — non-fatal

        for index, mapping in ((self._s.es_log_index, LOG_MAPPING),
                               (self._s.es_incident_index, INCIDENT_MAPPING)):
            if not await self.es.indices.exists(index=index):
                await self.es.indices.create(index=index, mappings=mapping)

    # --- writes -------------------------------------------------------------
    async def bulk_index_logs(self, events: Iterable[LogEvent]) -> int:
        """Bulk index events; event_id as _id makes re-ingest idempotent."""
        actions = (
            {
                "_op_type": "index",
                "_index": self._s.es_log_index,
                "_id": ev.event_id,
                **ev.model_dump(mode="json"),
            }
            for ev in events
        )
        ok, _ = await async_bulk(self.es, actions, chunk_size=self._s.es_bulk_size,
                                 raise_on_error=False)
        return ok

    async def upsert_incident(self, inc: Incident) -> None:
        """Upsert by fingerprint: merge counts/hosts if the incident exists."""
        await self.es.update(
            index=self._s.es_incident_index,
            id=inc.fingerprint,
            doc=inc.model_dump(mode="json"),
            doc_as_upsert=True,
        )

    # --- reads (also used by MCP tools) ------------------------------------
    async def search_logs(self, *, query: str | None = None, host: str | None = None,
                          subsystem: str | None = None, since_minutes: int = 60,
                          size: int = 50) -> list[dict[str, Any]]:
        must: list[dict[str, Any]] = [
            {"range": {"ts": {"gte": f"now-{since_minutes}m"}}}
        ]
        if query:
            must.append({"match": {"message": query}})
        if host:
            must.append({"term": {"host": host}})
        if subsystem:
            must.append({"term": {"subsystem": subsystem}})
        resp = await self.es.search(
            index=self._s.es_log_index,
            query={"bool": {"must": must}},
            sort=[{"ts": "desc"}],
            size=size,
        )
        return [h["_source"] for h in resp["hits"]["hits"]]

    async def signature_rate(self, signature: str, minutes: int, buckets: int) -> list[int]:
        """Per-bucket counts for a signature over the window (for anomaly calc)."""
        interval = max(1, minutes // buckets)
        resp = await self.es.search(
            index=self._s.es_log_index,
            size=0,
            query={"bool": {"must": [
                {"term": {"signature": signature}},
                {"range": {"ts": {"gte": f"now-{minutes}m"}}},
            ]}},
            aggs={"rate": {"date_histogram": {"field": "ts",
                                              "fixed_interval": f"{interval}m"}}},
        )
        return [b["doc_count"] for b in resp["aggregations"]["rate"]["buckets"]]

    async def similar_incidents(self, signature: str, size: int = 5) -> list[dict[str, Any]]:
        resp = await self.es.search(
            index=self._s.es_incident_index,
            query={"more_like_this": {
                "fields": ["signature", "sample_messages"],
                "like": signature, "min_term_freq": 1, "min_doc_freq": 1,
            }},
            size=size,
        )
        return [h["_source"] for h in resp["hits"]["hits"]]

    async def host_event_summary(self, host: str, minutes: int = 60) -> dict[str, int]:
        resp = await self.es.search(
            index=self._s.es_log_index,
            size=0,
            query={"bool": {"must": [
                {"term": {"host": host}},
                {"range": {"ts": {"gte": f"now-{minutes}m"}}},
            ]}},
            aggs={"by_sub": {"terms": {"field": "subsystem", "size": 20}}},
        )
        return {b["key"]: b["doc_count"]
                for b in resp["aggregations"]["by_sub"]["buckets"]}

    async def get_incident(self, fingerprint: str) -> Incident | None:
        try:
            doc = await self.es.get(index=self._s.es_incident_index, id=fingerprint)
        except NotFoundError:
            return None
        return Incident(**doc["_source"])
