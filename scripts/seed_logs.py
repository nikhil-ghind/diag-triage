#!/usr/bin/env python3
"""Generate synthetic kernel/driver diagnostic logs and push them through the
ingest pipeline — useful for demoing the end-to-end flow without real hardware.

Emits a baseline of benign chatter plus a few injectable failure scenarios:
  * gpu-xid     : NVIDIA Xid 79 (GPU fell off the bus) across several hosts
  * ecc-ramp    : correctable ECC errors accelerating on one host (rate spike)
  * nvme-reset  : NVMe controller resets/timeouts
  * mce         : uncorrected machine-check on one host

Usage:
    python scripts/seed_logs.py --scenario gpu-xid --hosts 8
    python scripts/seed_logs.py --scenario ecc-ramp --hosts 1
"""
from __future__ import annotations

import argparse
import asyncio
import random

from triage.app.service import TriageService
from triage.config import get_settings

BENIGN = [
    "systemd[1]: Started Daily apt download activities.",
    "kernel: [ {t}.123] usb 1-1: new high-speed USB device number {n}",
    "chronyd[812]: Selected source 10.0.0.{n}",
]


def gpu_xid(host: str, t: float) -> list[str]:
    return [
        f"kernel: [ {t}.001] NVRM: Xid (PCI:0000:3b:00): 79, pid=0, GPU has fallen off the bus.",
        f"kernel: [ {t}.002] NVRM: GPU 0000:3b:00.0: GPU has fallen off the bus.",
    ]


def ecc_ramp(host: str, t: float, intensity: int) -> list[str]:
    return [f"kernel: [ {t + i*0.01:.3f}] EDAC MC0: 1 CE memory read error on "
            f"DIMM_A{random.randint(0,3)} (channel:0 page:0x{random.randint(0,1<<20):x})"
            for i in range(intensity)]


def nvme_reset(host: str, t: float) -> list[str]:
    return [
        f"kernel: [ {t}.001] nvme nvme0: I/O 12 QID 3 timeout, reset controller",
        f"kernel: [ {t}.500] nvme nvme0: Abort status: 0x0",
    ]


def mce(host: str, t: float) -> list[str]:
    return [
        f"kernel: [ {t}.001] mce: [Hardware Error]: Machine check events logged",
        f"kernel: [ {t}.002] mce: [Hardware Error]: CPU 4: Uncorrected error",
    ]


SCENARIOS = {"gpu-xid": gpu_xid, "ecc-ramp": ecc_ramp, "nvme-reset": nvme_reset, "mce": mce}


async def main_async(args: argparse.Namespace) -> None:
    svc = TriageService(get_settings())
    await svc.startup()
    t0 = random.uniform(1000, 9000)
    for h in range(args.hosts):
        host = f"node-{h:03d}"
        lines = [random.choice(BENIGN).format(t=t0, n=random.randint(1, 99))
                 for _ in range(args.benign)]
        if args.scenario == "ecc-ramp":
            lines += ecc_ramp(host, t0, args.intensity)
        else:
            lines += SCENARIOS[args.scenario](host, t0)
        result = await svc.ingest(host, lines)
        print(host, result)
    await svc.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="gpu-xid")
    ap.add_argument("--hosts", type=int, default=4)
    ap.add_argument("--benign", type=int, default=10)
    ap.add_argument("--intensity", type=int, default=30, help="lines for ecc-ramp")
    asyncio.run(main_async(ap.parse_args()))
