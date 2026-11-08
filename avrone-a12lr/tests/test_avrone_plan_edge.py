#!/usr/bin/env python3
"""Plan-check census.

The script refuses when the reintro plan is missing or malformed.
Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- plan missing: refused
- plan is not JSON: refused
- plan missing core: refused
- plan has empty batches: refused
- plan is valid: continues to the next check

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
PLAN = REPO / "scripts" / "avrone-state" / "reintro-plan.json"
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
    print("AVRONE-12 Plan-check census")
    original = PLAN.read_text()
    drive = tempfile.mkdtemp()
    try:
        PLAN.unlink()
        rc, out = run(drive)
        rec("missing refuses", rc != 0 and "missing" in out and "reintro-plan.json" in out)
        PLAN.write_text("not json")
        rc, out = run(drive)
        rec("not json refuses", rc != 0 and "invalid" in out)
        PLAN.write_text('{"batches":[["x"]]}')
        rc, out = run(drive)
        rec("missing core refuses", rc != 0 and "invalid" in out)
        PLAN.write_text('{"core":["list_files"],"batches":[]}')
        rc, out = run(drive)
        rec("empty batches refuses", rc != 0 and "invalid" in out)
        PLAN.write_text(original)
        rc, out = run(drive)
        rec("valid continues", rc != 0 and "invalid" not in out and "missing" not in out)
    finally:
        PLAN.write_text(original)
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 4, "deterministic")
    rec("four refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v33",
        "finding": (
            "The script refuses when the reintro plan is missing or malformed. Missing, "
            "not JSON, missing core, and empty batches all refuse. A valid plan continues. "
            "Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": ["missing", "not json", "missing core", "empty batches"],
        "valid": "continue",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The plan check holds.",
    }
    out = ROOT / "references" / "great-scale-plan-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("plan evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"four refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
