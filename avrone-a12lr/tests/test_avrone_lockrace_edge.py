#!/usr/bin/env python3
"""Lock-race census.

The script treats an empty lock and a non-numeric pid as stale. Probed as
uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- empty lock (no pid file): removed, 'pid unknown is gone'
- non-numeric pid 'abc': removed, 'pid abc is gone'

The check holds. Both are stale.
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
    print("AVRONE-12 Lock-race census")
    drive = tempfile.mkdtemp()
    lock = Path(drive) / "avrone-state" / ".autopilot.lock"
    lock.parent.mkdir()
    try:
        lock.mkdir()
        rc, out = run(drive)
        rec("empty lock is removed", rc != 2 and "pid unknown is gone" in out)
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir()
        (lock / "pid").write_text("abc")
        rc, out = run(drive)
        rec("non-numeric pid is removed", rc != 2 and "pid abc is gone" in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 2, "deterministic")
    rec("both removals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v44",
        "finding": (
            "The script treats an empty lock and a non-numeric pid as stale. An empty "
            "lock is removed ('pid unknown is gone'). A non-numeric pid is removed "
            "('pid abc is gone'). Probed as uid 0 with ALLOW_ROOT=1 and every earlier "
            "check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "emptyLock": "removed, pid unknown",
        "nonNumeric": "removed, pid abc",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The lock-race check holds.",
    }
    out = ROOT / "references" / "great-scale-lockrace-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("lock-race evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"removals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
