"""Severity-aware alert routing with dedup + throttling.

Routing policy:
  * critical -> PagerDuty (page) + Slack
  * error    -> Slack
  * warning  -> Slack only if the agent's confidence is high enough
  * info     -> log sink only

Each (fingerprint) is throttled: once an alert for a signature fires, duplicates
are suppressed for `alert_throttle_s`. This is what keeps a 500-host GPU fault
from emitting 500 pages — the clustering collapses it to one incident, and the
throttle collapses recurrences over time.
"""
from __future__ import annotations

import time

import structlog

from triage.config import Settings
from triage.models import Alert, Incident, Severity, Triage
from triage.routing.sinks import LogSink, PagerDutySink, SlackSink

log = structlog.get_logger(__name__)


class AlertRouter:
    def __init__(self, settings: Settings):
        self._s = settings
        self._log_sink = LogSink()
        self._slack = SlackSink(settings.slack_webhook_url) if settings.slack_webhook_url else None
        self._pd = PagerDutySink(settings.pagerduty_routing_key) if settings.pagerduty_routing_key else None
        # fingerprint -> last-sent monotonic timestamp
        self._last_sent: dict[str, float] = {}

    def _throttled(self, fingerprint: str) -> bool:
        now = time.monotonic()
        last = self._last_sent.get(fingerprint)
        if last is not None and (now - last) < self._s.alert_throttle_s:
            return True
        self._last_sent[fingerprint] = now
        return False

    def _channels_for(self, severity: Severity, triage: Triage) -> list[str]:
        if severity == Severity.critical:
            return ["pagerduty", "slack"]
        if severity == Severity.error:
            return ["slack"]
        if severity == Severity.warning and triage.confidence >= 0.6:
            return ["slack"]
        return ["log"]

    def _render(self, incident: Incident, triage: Triage, channel: str) -> Alert:
        hosts = ", ".join(incident.hosts[:8]) + (
            f" (+{len(incident.hosts) - 8} more)" if len(incident.hosts) > 8 else "")
        title = f"[{incident.subsystem.value.upper()}] {triage.summary[:120]}"
        body = (
            f"*Summary:* {triage.summary}\n"
            f"*Probable cause:* {triage.probable_cause}\n"
            f"*Recommended action:* {triage.recommended_action}\n"
            f"*Blast radius:* {triage.blast_radius}\n"
            f"*Hosts:* {hosts}\n"
            f"*Confidence:* {triage.confidence:.0%}  ·  *Events:* {incident.count}\n"
            f"*Signature:* `{incident.signature}`"
        )
        if triage.citations:
            body += f"\n*Evidence:* {', '.join(triage.citations[:5])}"
        return Alert(
            incident_id=incident.incident_id, severity=incident.severity,
            title=title, body=body, channel=channel, fingerprint=incident.fingerprint,
        )

    async def route(self, incident: Incident, triage: Triage) -> list[Alert]:
        if self._throttled(incident.fingerprint):
            log.info("alert_throttled", fingerprint=incident.fingerprint,
                     incident=incident.incident_id)
            return []

        sent: list[Alert] = []
        for channel in self._channels_for(incident.severity, triage):
            alert = self._render(incident, triage, channel)
            sink = {"slack": self._slack, "pagerduty": self._pd}.get(channel)
            if sink is None:  # unconfigured channel -> degrade to log
                sink = self._log_sink
                alert.channel = "log"
            if await sink.send(alert):
                sent.append(alert)
        return sent
