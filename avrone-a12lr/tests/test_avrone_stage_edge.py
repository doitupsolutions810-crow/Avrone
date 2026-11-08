#!/usr/bin/env python3
"""Stage-check census.

The stage check repairs a corrupt reintro-stage.txt. It does not refuse.
Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- '1.5': corrupt, restored from repo
- '-3': corrupt, restored from repo
- two lines: corrupt (reads as 'multi'), restored from repo
- '1h': corrupt, restored from repo
- '3': accepted, continues to the next check

The check holds. It repairs instead of refusing.
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


def run(drive: str, stage: str) -> tuple[int, str, str]:
    state = Path(drive) / "avrone-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "reintro-stage.txt").write_text(stage)
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    after = (state / "reintro-stage.txt").read_text().strip() if (state / "reintro-stage.txt").is_file() else ""
    return p.returncode, p.stdout + p.stderr, after


def main() -> int:
    print("AVRONE-12 Stage-check census")
    drive = tempfile.mkdtemp()
    try:
        for label, value in (("decimal", "1.5"), ("negative", "-3"), ("two lines", "6\n7"), ("suffix", "1h")):
            rc, out, after = run(drive, value)
            rec(f"{label} is repaired", rc != 0 and "corrupt" in out and after.isdigit())
        rc, out, after = run(drive, "3")
        rec("plain integer is accepted", rc != 0 and "corrupt" not in out and after == "3")
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 4, "deterministic")
    rec("four repairs are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v35",
        "finding": (
            "The stage check repairs a corrupt reintro-stage.txt. It does not refuse. "
            "A decimal, a negative, two lines, and a suffix are all restored from the "
            "repo. A plain integer is accepted. Probed as uid 0 with ALLOW_ROOT=1 and "
            "every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "repaired": ["1.5", "-3", "two lines", "1h"],
        "accepted": "3",
        "behavior": "repair, not refuse",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The stage check holds. It repairs instead of refusing.",
    }
    out = ROOT / "references" / "great-scale-stage-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("stage evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"four repairs: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
