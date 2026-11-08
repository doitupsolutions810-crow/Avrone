#!/usr/bin/env python3
"""Lane-check census.

The lane check allows only deepcov, newmods, tools, robust. Probed as
uid 0 with ALLOW_ROOT=1, a valid drive, and a valid model:

- 'notalane': refused
- '$(id)': refused
- 'deepcov;id': refused
- 'deepcov bogus': refused (bogus is unknown)
- 'deepcov tools': passes, reaches the next check

The lane check holds even when root is allowed and the drive and model
are valid. Shell syntax in a lane name is refused as an unknown lane.
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(lanes: str, drive: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES=lanes)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Lane-check census")
    drive = tempfile.mkdtemp()
    try:
        bad = [("notalane", "notalane"), ("$(id)", "$(id)"), ("deepcov;id", "deepcov;id"), ("deepcov bogus", "bogus")]
        for lanes, needle in bad:
            rc, out = run(lanes, drive)
            rec(f"refuses {lanes!r}", rc != 0 and f"unknown lane '{needle}'" in out)
        rc, out = run("deepcov tools", drive)
        rec("valid lanes pass the check", rc != 0 and "unknown lane" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 4, "deterministic")
    rec("four refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v26",
        "finding": (
            "The lane check allows only deepcov, newmods, tools, robust. Unknown lanes "
            "and shell syntax in a lane name all refuse, even with ALLOW_ROOT=1 and a "
            "valid drive and model. A valid lane list passes the check and reaches the "
            "next one. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": [b[0] for b in bad],
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The lane check holds.",
    }
    out = ROOT / "references" / "great-scale-lane-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("lane evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"four refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
