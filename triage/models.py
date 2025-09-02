"""Domain models for the triage pipeline.

The data flow is:  raw line -> LogEvent -> (detector) Failure -> (clusterer)
Incident -> (agent) Triage -> (router) Alert.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2, "critical": 3}[self.value]


class Subsystem(str, Enum):
    """Kernel/driver subsystem the log originated from."""

    gpu = "gpu"          # NVIDIA Xid, DRM, nvidia-smi
    nvme = "nvme"        # storage controller
    mce = "mce"          # machine-check exceptions
    network = "network"  # NIC driver, link flaps
    memory = "memory"    # EDAC / ECC
    thermal = "thermal"  # thermal throttling
    kernel = "kernel"    # generic dmesg
    unknown = "unknown"


class LogEvent(BaseModel):
    """A single parsed log line ready for indexing."""

    ts: datetime = Field(default_factory=_utcnow)
    host: str
    subsystem: Subsystem = Subsystem.unknown
    severity: Severity = Severity.info
    message: str
    raw: str
    # Stable per-line id (host+raw+ts) so re-ingesting is idempotent.
    event_id: str = ""
    # Normalized template (numbers/addresses stripped) for grouping.
    signature: str = ""
    fields: dict[str, str] = Field(default_factory=dict)

    def finalize(self) -> "LogEvent":
        if not self.event_id:
            h = hashlib.sha1(f"{self.host}|{self.ts.isoformat()}|{self.raw}".encode())
            self.event_id = h.hexdigest()
        return self


class Failure(BaseModel):
    """A LogEvent the detector flagged as a hardware/driver failure."""

    event: LogEvent
    rule: str                      # which detection rule fired
    signature: str                 # normalized template (drives clustering)
    severity: Severity
    anomalous_rate: bool = False   # also flagged by the rate-spike detector


class Incident(BaseModel):
    """A cluster of correlated failures across hosts within a time window."""

    incident_id: str
    signature: str
    subsystem: Subsystem
    severity: Severity
    opened_at: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)
    count: int = 0
    hosts: list[str] = Field(default_factory=list)
    sample_messages: list[str] = Field(default_factory=list)
    fingerprint: str = ""          # dedup key for routing/throttling

    def fingerprint_key(self) -> str:
        return hashlib.sha1(f"{self.subsystem.value}|{self.signature}".encode()).hexdigest()[:16]


class Triage(BaseModel):
    """The agent's analysis of an incident."""

    incident_id: str
    summary: str
    probable_cause: str
    recommended_action: str
    blast_radius: str              # e.g. "12 hosts in rack A3"
    confidence: float = 0.0
    related_incident_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)  # event_ids the agent used
    model: str = ""
    tool_calls: int = 0


class Alert(BaseModel):
    """A routed, deliverable alert."""

    incident_id: str
    severity: Severity
    title: str
    body: str
    channel: str                   # "slack" | "pagerduty" | "log"
    fingerprint: str
    created_at: datetime = Field(default_factory=_utcnow)
