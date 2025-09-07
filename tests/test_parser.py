"""Parser: subsystem/severity classification and signature normalization."""
from triage.ingest.parser import classify, normalize_signature, parse_line
from triage.models import Severity, Subsystem


def test_classify_gpu_xid_is_critical():
    sub, sev, fields = classify("NVRM: Xid (PCI:0000:3b:00): 79, GPU has fallen off the bus.")
    assert sub == Subsystem.gpu
    assert sev == Severity.critical
    assert fields.get("xid") == "79"
    assert fields.get("pci") == "3b:00.0" or "pci" in fields  # pci captured when present


def test_classify_ecc_is_memory_error():
    sub, sev, _ = classify("EDAC MC0: 1 CE memory read error on DIMM_A1")
    assert sub == Subsystem.memory
    assert sev == Severity.error


def test_classify_mce_uncorrected_is_critical():
    sub, sev, _ = classify("mce: [Hardware Error]: CPU 4: Uncorrected error")
    assert sub == Subsystem.mce
    assert sev == Severity.critical


def test_signature_strips_volatile_tokens():
    a = normalize_signature("[ 1234.567] nvme nvme0: I/O 12 QID 3 timeout, reset controller")
    b = normalize_signature("[ 9999.001] nvme nvme7: I/O 88 QID 9 timeout, reset controller")
    # Different instances of the same failure collapse to one signature.
    assert a == b
    assert "<n>" in a


def test_signature_distinguishes_different_failures():
    a = normalize_signature("nvme nvme0: timeout, reset controller")
    b = normalize_signature("EDAC MC0: CE memory read error")
    assert a != b


def test_parse_line_finalizes_event_id():
    ev = parse_line("node-001", "[ 12.3] NVRM: Xid (PCI:0000:3b:00): 79, fault")
    assert ev.event_id  # populated by finalize()
    assert ev.subsystem == Subsystem.gpu
    assert ev.host == "node-001"
