"""Detector rate-anomaly math + clustering into incidents."""
from triage.config import Settings
from triage.ingest.detector import Detector, rate_zscore
from triage.ingest.parser import parse_line
from triage.models import Failure, Severity, Subsystem


def test_rate_zscore_flat_is_zero():
    assert rate_zscore([2, 2, 2, 2]) == 0.0


def test_rate_zscore_spike_is_large():
    assert rate_zscore([1, 1, 1, 1, 20]) > 3.0


def test_rate_zscore_insufficient_data():
    assert rate_zscore([5]) == 0.0
    assert rate_zscore([5, 9]) == 0.0


def _failure(host, msg, sev, sub, sig):
    ev = parse_line(host, msg)
    ev.severity, ev.subsystem, ev.signature = sev, sub, sig
    return Failure(event=ev, rule="subsystem_severity", signature=sig, severity=sev)


def test_cluster_groups_by_signature_and_collects_hosts():
    s = Settings(min_cluster_size=2)
    det = Detector(s, es=None)  # cluster() doesn't touch ES
    fails = [
        _failure("n1", "Xid 79", Severity.critical, Subsystem.gpu, "nvrm: xid <n>"),
        _failure("n2", "Xid 79", Severity.critical, Subsystem.gpu, "nvrm: xid <n>"),
        _failure("n3", "nvme timeout", Severity.error, Subsystem.nvme, "nvme timeout"),
    ]
    incidents = det.cluster(fails)
    gpu = [i for i in incidents if i.subsystem == Subsystem.gpu]
    assert len(gpu) == 1
    assert set(gpu[0].hosts) == {"n1", "n2"}
    assert gpu[0].count == 2
    assert gpu[0].fingerprint  # set during clustering
    # Lone nvme error is below min_cluster_size and not critical -> dropped.
    assert not any(i.subsystem == Subsystem.nvme for i in incidents)


def test_cluster_keeps_lone_critical():
    s = Settings(min_cluster_size=3)
    det = Detector(s, es=None)
    fails = [_failure("n9", "panic", Severity.critical, Subsystem.kernel, "kernel panic")]
    incidents = det.cluster(fails)
    assert len(incidents) == 1  # critical bypasses min_cluster_size
