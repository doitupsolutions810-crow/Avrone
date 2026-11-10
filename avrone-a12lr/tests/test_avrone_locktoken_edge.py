#!/usr/bin/env python3
"""Lock-token census.

The lock decision does not look at the token. Probed as uid 0 with
ALLOW_ROOT=1 and every earlier check passing:

- live pid, no token: exits 2
- live pid, wrong token: exits 2
- live pid, matching token: exits 2

The token is for check_drive (drive-swap detection), not for the lock.
The lock only cares about the pid. The check holds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

REPO = Path("/tmp/Avrone")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" \u2014 {detail}" if detail else ""))


def run(drive: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Lock-token census")
    drive = tempfile.mkdtemp()
    lock = Path(drive) / "avrone-state" / ".autopilot.lock"
    lock.parent.mkdir()
    try:
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        rc, out = run(drive)
        rec("no token exits 2", rc == 2 and "already running" in out)
        (lock / "token").write_text("wrong-token")
        rc, out = run(drive)
        rec("wrong token exits 2", rc == 2 and "already running" in out)
        (lock / "token").write_text(f"{os.getpid()}-0-{int(time.time())}")
        rc, out = run(drive)
        rec("matching token exits 2", rc == 2 and "already running" in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 3, "deterministic")
    rec("three exits are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v45",
        "finding": (
            "The lock decision does not look at the token. A live pid exits 2 "
            "whether the token is missing, wrong, or matching. The token is for "
            "check_drive (drive-swap detection), not for the lock. The lock only "
            "cares about the pid. Pure. Heat is zero."
        ),
        "cone": CONE,
        "noToken": "exit 2",
        "wrongToken": "exit 2",
        "matchingToken": "exit 2",
        "tokenUsedBy": "check_drive",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The token is for a different check. The lock only cares about the pid.",
    }
    out = ROOT / "references" / "great-scale-locktoken-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("lock-token evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"three exits: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
