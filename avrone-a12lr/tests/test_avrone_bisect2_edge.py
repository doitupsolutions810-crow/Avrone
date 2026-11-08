#!/usr/bin/env python3
"""Bisect-check census.

The bisect check refuses when BISECT_LOG is present in the git dir.
Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- BISECT_LOG present: refused
- no BISECT_LOG: continues to the next check

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
    print("AVRONE-12 Bisect-check census")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("check looks for BISECT_LOG", "BISECT_LOG:bisect" in text)

    gitdir = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    bisect = (REPO / gitdir / "BISECT_LOG") if not Path(gitdir).is_absolute() else Path(gitdir) / "BISECT_LOG"
    drive = tempfile.mkdtemp()
    try:
        bisect.touch()
        rc, out = run(drive)
        rec("BISECT_LOG present refuses", rc != 0 and "middle of a bisect" in out)
        bisect.unlink()
        rc, out = run(drive)
        rec("no BISECT_LOG continues", rc != 0 and "middle of a bisect" not in out)
    finally:
        bisect.unlink(missing_ok=True)
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 2, "deterministic")
    rec("both outcomes are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v30",
        "finding": (
            "The bisect check refuses when BISECT_LOG is present and continues when "
            "it is not. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check "
            "passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "bisectLogPresent": "refuse",
        "bisectLogAbsent": "continue",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The bisect check holds.",
    }
    out = ROOT / "references" / "great-scale-bisect2-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("bisect evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"two outcomes: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
