"""Parsers for kernel/driver hardware diagnostic logs.

Turns raw lines (dmesg, NVIDIA Xid, EDAC, nvme, MCE, syslog) into LogEvents:
classifies the subsystem, extracts a severity, and computes a normalized
*signature* — the message with volatile tokens (addresses, counters, PCI ids,
timestamps) masked — so that physically-distinct-but-logically-identical
failures collapse to one group.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from triage.models import LogEvent, Severity, Subsystem

# --- signature normalization ------------------------------------------------
# Order matters: most-specific patterns first.
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\b"), "<pci>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]

_DMESG_TS = re.compile(r"^\[\s*\d+\.\d+\]\s*")  # "[ 1234.567]" kernel timestamp


def normalize_signature(message: str) -> str:
    sig = _DMESG_TS.sub("", message).strip()
    for pat, repl in _NORMALIZERS:
        sig = pat.sub(repl, sig)
    return re.sub(r"\s+", " ", sig).strip().lower()


# --- subsystem / severity rules ---------------------------------------------
# Each rule: (subsystem, severity, compiled regex, optional field extractor).
_RULES: list[tuple[Subsystem, Severity, re.Pattern[str]]] = [
    (Subsystem.gpu, Severity.critical, re.compile(r"\bNVRM:.*Xid", re.I)),
    (Subsystem.gpu, Severity.critical, re.compile(r"\bGPU has fallen off the bus", re.I)),
    (Subsystem.gpu, Severity.error, re.compile(r"\bnvidia-smi.*(ERR!|Unknown Error)", re.I)),
    (Subsystem.mce, Severity.critical, re.compile(r"\bmce:.*Hardware Error", re.I)),
    (Subsystem.mce, Severity.critical, re.compile(r"\bMachine check events logged", re.I)),
    (Subsystem.memory, Severity.error, re.compile(r"\bEDAC.*(CE|UE)\b")),
    (Subsystem.memory, Severity.critical, re.compile(r"\bUncorrected error", re.I)),
    (Subsystem.nvme, Severity.error, re.compile(r"\bnvme\d+:.*(I/O|timeout|reset)", re.I)),
    (Subsystem.nvme, Severity.critical, re.compile(r"\bnvme.*controller is down", re.I)),
    (Subsystem.thermal, Severity.warning, re.compile(r"\b(thermal|throttl)", re.I)),
    (Subsystem.network, Severity.warning, re.compile(r"\b(Link is Down|NIC Link)", re.I)),
    (Subsystem.network, Severity.error, re.compile(r"\b(tx|rx) hang|Detected Hardware Unit Hang", re.I)),
    (Subsystem.kernel, Severity.critical, re.compile(r"\bkernel panic", re.I)),
    (Subsystem.kernel, Severity.error, re.compile(r"\bcall trace:|\bBUG:|\boops", re.I)),
]

# Pull the NVIDIA Xid code out when present — it's the single most useful field.
_XID = re.compile(r"Xid.*?:?\s*(\d{1,4})", re.I)
_PCI = re.compile(r"\b([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\b")


def classify(message: str) -> tuple[Subsystem, Severity, dict[str, str]]:
    fields: dict[str, str] = {}
    if m := _PCI.search(message):
        fields["pci"] = m.group(1)
    if m := _XID.search(message):
        fields["xid"] = m.group(1)
    for subsystem, severity, pat in _RULES:
        if pat.search(message):
            return subsystem, severity, fields
    # default: severity from common syslog keywords
    sev = Severity.info
    low = message.lower()
    if "error" in low or "fail" in low:
        sev = Severity.error
    elif "warn" in low:
        sev = Severity.warning
    return Subsystem.unknown, sev, fields


def parse_line(host: str, raw: str, ts: datetime | None = None) -> LogEvent:
    """Parse one raw log line into a finalized LogEvent."""
    message = _DMESG_TS.sub("", raw).strip()
    subsystem, severity, fields = classify(message)
    ev = LogEvent(
        ts=ts or datetime.now(timezone.utc),
        host=host,
        subsystem=subsystem,
        severity=severity,
        message=message,
        raw=raw,
        signature=normalize_signature(message),
        fields=fields,
    )
    return ev.finalize()
