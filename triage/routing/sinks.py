"""Alert delivery sinks. Each sink knows how to format+POST an Alert.

Sinks are best-effort and never raise into the pipeline: a delivery failure is
logged and swallowed so one broken webhook can't stall ingestion.
"""
from __future__ import annotations

import httpx
import structlog

from triage.models import Alert, Severity

log = structlog.get_logger(__name__)


class Sink:
    name = "base"

    async def send(self, alert: Alert) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class LogSink(Sink):
    """Always-available fallback — structured log line."""

    name = "log"

    async def send(self, alert: Alert) -> bool:
        log.warning("alert", channel="log", severity=alert.severity.value,
                    title=alert.title, incident=alert.incident_id)
        return True


class SlackSink(Sink):
    name = "slack"

    _EMOJI = {Severity.info: ":information_source:", Severity.warning: ":warning:",
              Severity.error: ":rotating_light:", Severity.critical: ":fire:"}

    def __init__(self, webhook_url: str):
        self._url = webhook_url

    async def send(self, alert: Alert) -> bool:
        payload = {
            "text": f"{self._EMOJI.get(alert.severity, '')} *{alert.title}*",
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": alert.title[:150]}},
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": alert.body[:2900]}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn",
                     "text": f"incident `{alert.incident_id}` · severity "
                             f"*{alert.severity.value}* · fp `{alert.fingerprint}`"}]},
            ],
        }
        return await self._post(payload)

    async def _post(self, payload: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(self._url, json=payload)
                r.raise_for_status()
            return True
        except Exception as exc:
            log.error("slack_send_failed", err=str(exc))
            return False


class PagerDutySink(Sink):
    """PagerDuty Events API v2 — only critical/error get paged."""

    name = "pagerduty"
    _URL = "https://events.pagerduty.com/v2/enqueue"
    _SEV = {Severity.warning: "warning", Severity.error: "error",
            Severity.critical: "critical", Severity.info: "info"}

    def __init__(self, routing_key: str):
        self._key = routing_key

    async def send(self, alert: Alert) -> bool:
        payload = {
            "routing_key": self._key,
            "event_action": "trigger",
            "dedup_key": alert.fingerprint,  # PD-side dedup mirrors ours
            "payload": {
                "summary": alert.title[:1024],
                "source": "diag-triage",
                "severity": self._SEV.get(alert.severity, "error"),
                "custom_details": {"body": alert.body, "incident": alert.incident_id},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(self._URL, json=payload)
                r.raise_for_status()
            return True
        except Exception as exc:
            log.error("pagerduty_send_failed", err=str(exc))
            return False
