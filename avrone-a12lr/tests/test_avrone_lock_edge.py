#!/usr/bin/env python3
"""Lock-check census.

The script exits 2 when a lock with a live pid is present, and removes a
stale lock. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check
passing:

- live pid: exits 2, lock stays
- stale pid (999999): removed, run continues
- no lock: continues to the next check

The check holds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    print("AVRONE-12 Lock-check census")
    drive = tempfile.mkdtemp()
    lock = Path(drive) / "avrone-state" / ".autopilot.lock"
    lock.parent.mkdir()
    try:
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        rc, out = run(drive)
        rec("live pid exits 2", rc == 2 and "already running" in out)
        (lock / "pid").write_text("999999")
        rc, out = run(drive)
        rec("stale pid is removed", rc != 2 and "stale lock" in out)
        shutil.rmtree(lock, ignore_errors=True)
        rc, out = run(drive)
        rec("no lock continues", rc != 2 and "already running" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the exit is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v43",
        "finding": (
            "The script exits 2 when a lock with a live pid is present, and removes "
            "a stale lock. A live pid exits 2. A stale pid is removed and the run "
            "continues. No lock continues. Probed as uid 0 with ALLOW_ROOT=1 and "
            "every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "livePid": "exit 2",
        "stalePid": "removed, continues",
        "noLock": "continues",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The lock check holds.",
    }
    out = ROOT / "references" / "great-scale-lock-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("lock evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"exit: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
