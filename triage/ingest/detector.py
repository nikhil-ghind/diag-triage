"""Failure detection + clustering into incidents.

Two detection signals:
  1. Rule-based — the parser already tagged subsystem/severity; anything at
     `error`+ severity from a hardware subsystem is a failure.
  2. Rate anomaly — a per-signature spike vs its recent baseline (z-score over
     date-histogram buckets), which catches floods of individually-"warning"
     lines (e.g. correctable ECC errors ramping toward an uncorrectable one).

Failures are then clustered by (subsystem, signature) into Incidents within a
correlation window, accumulating affected hosts and counts.
"""
from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone

from triage.config import Settings
from triage.ingest.es_client import ESClient
from triage.models import Failure, Incident, LogEvent, Severity, Subsystem

_HARDWARE_SUBSYSTEMS = {
    Subsystem.gpu, Subsystem.nvme, Subsystem.mce,
    Subsystem.memory, Subsystem.thermal, Subsystem.network,
}


def rate_zscore(counts: list[int]) -> float:
    """Z-score of the latest bucket vs the preceding ones. 0 if too little data."""
    if len(counts) < 3:
        return 0.0
    *baseline, latest = counts
    if len(baseline) < 2:
        return 0.0
    mu = statistics.mean(baseline)
    sd = statistics.pstdev(baseline)
    if sd == 0:
        return 0.0 if latest <= mu else float("inf")
    return (latest - mu) / sd


class Detector:
    def __init__(self, settings: Settings, es: ESClient):
        self._s = settings
        self._es = es

    def is_failure(self, ev: LogEvent) -> bool:
        return (ev.severity.rank >= Severity.error.rank
                and ev.subsystem in _HARDWARE_SUBSYSTEMS) or \
               ev.severity == Severity.critical

    async def detect(self, events: list[LogEvent]) -> list[Failure]:
        """Classify a batch into failures, enriching with rate-anomaly flags."""
        failures: list[Failure] = []
        # Cache rate lookups per signature within a batch.
        rate_cache: dict[str, bool] = {}
        for ev in events:
            anomalous = False
            if ev.signature and ev.severity.rank >= Severity.warning.rank:
                if ev.signature not in rate_cache:
                    counts = await self._es.signature_rate(
                        ev.signature, minutes=self._s.detect_window_s // 60 * 6 or 30,
                        buckets=6)
                    rate_cache[ev.signature] = (
                        rate_zscore(counts) >= self._s.rate_spike_zscore)
                anomalous = rate_cache[ev.signature]

            if self.is_failure(ev) or anomalous:
                sev = ev.severity
                if anomalous and sev.rank < Severity.error.rank:
                    sev = Severity.error  # escalate a spiking warning
                failures.append(Failure(
                    event=ev,
                    rule="rate_spike" if (anomalous and not self.is_failure(ev)) else "subsystem_severity",
                    signature=ev.signature,
                    severity=sev,
                    anomalous_rate=anomalous,
                ))
        return failures

    def cluster(self, failures: list[Failure]) -> list[Incident]:
        """Group failures by (subsystem, signature) into Incidents."""
        groups: dict[tuple[Subsystem, str], list[Failure]] = {}
        for f in failures:
            groups.setdefault((f.event.subsystem, f.signature), []).append(f)

        incidents: list[Incident] = []
        for (subsystem, signature), members in groups.items():
            if len(members) < self._s.min_cluster_size and \
               not any(m.severity == Severity.critical for m in members):
                continue  # too sparse and not critical — hold off
            hosts = sorted({m.event.host for m in members})
            sev = max((m.severity for m in members), key=lambda s: s.rank)
            ts_sorted = sorted(members, key=lambda m: m.event.ts)
            inc = Incident(
                incident_id=str(uuid.uuid4()),
                signature=signature,
                subsystem=subsystem,
                severity=sev,
                opened_at=ts_sorted[0].event.ts,
                last_seen=ts_sorted[-1].event.ts,
                count=len(members),
                hosts=hosts,
                sample_messages=[m.event.message for m in members[:3]],
            )
            inc.fingerprint = inc.fingerprint_key()
            incidents.append(inc)
        return incidents
