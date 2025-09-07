"""Router severity mapping, throttling, and rendering."""
import pytest

from triage.config import Settings
from triage.models import Incident, Severity, Subsystem, Triage
from triage.routing.router import AlertRouter


def _incident(sev=Severity.critical, fp="fp-abc"):
    inc = Incident(incident_id="i1", signature="nvrm: xid <n>",
                   subsystem=Subsystem.gpu, severity=sev, count=5,
                   hosts=["n1", "n2"], fingerprint=fp)
    return inc


def _triage(conf=0.9):
    return Triage(incident_id="i1", summary="GPU fell off bus", probable_cause="HW",
                  recommended_action="RMA", blast_radius="2 hosts", confidence=conf,
                  citations=["ev1", "ev2"])


def test_channels_critical_pages_and_slacks():
    r = AlertRouter(Settings())
    assert r._channels_for(Severity.critical, _triage()) == ["pagerduty", "slack"]


def test_channels_warning_needs_confidence():
    r = AlertRouter(Settings())
    assert r._channels_for(Severity.warning, _triage(conf=0.9)) == ["slack"]
    assert r._channels_for(Severity.warning, _triage(conf=0.2)) == ["log"]


async def test_route_degrades_to_log_when_unconfigured():
    # No slack/pagerduty configured -> all channels degrade to the log sink.
    r = AlertRouter(Settings(slack_webhook_url=None, pagerduty_routing_key=None))
    alerts = await r.route(_incident(), _triage())
    assert alerts
    assert all(a.channel == "log" for a in alerts)


async def test_route_throttles_duplicate_fingerprint():
    r = AlertRouter(Settings(alert_throttle_s=999))
    first = await r.route(_incident(fp="dup"), _triage())
    second = await r.route(_incident(fp="dup"), _triage())
    assert first        # first fires
    assert second == [] # duplicate within window suppressed


def test_render_includes_evidence_and_hosts():
    r = AlertRouter(Settings())
    alert = r._render(_incident(), _triage(), "slack")
    assert "n1" in alert.body
    assert "ev1" in alert.body
    assert alert.fingerprint == "fp-abc"
