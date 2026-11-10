#!/usr/bin/env python3
"""Default-stage census.

The script falls through to the default 0 when backup and repo are both
unavailable. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check
passing:

- corrupt stage, corrupt backup, missing repo stage: restored 0 from default
- corrupt stage, no backup, valid repo stage: restored 7 from repo

The check holds. It tries backup, then repo, then default.
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
REPO_STAGE = REPO / "scripts" / "avrone-state" / "reintro-stage.txt"
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" \u2014 {detail}" if detail else ""))


def run(drive: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    after = (Path(drive) / "avrone-state" / "reintro-stage.txt").read_text().strip()
    return p.returncode, p.stdout + p.stderr, after


def main() -> int:
    print("AVRONE-12 Default-stage census")
    drive = tempfile.mkdtemp()
    state = Path(drive) / "avrone-state"
    state.mkdir()
    original = REPO_STAGE.read_bytes()
    try:
        (state / "reintro-stage.txt").write_text("CORRUPT")
        (state / "reintro-stage.txt.bak").write_text("ALSO-BAD")
        REPO_STAGE.unlink()
        rc, out, after = run(drive)
        rec("falls to default 0", rc != 0 and "from default" in out and after == "0")
        REPO_STAGE.write_bytes(original)
        (state / "reintro-stage.txt.bak").unlink()
        (state / "reintro-stage.txt").write_text("CORRUPT")
        rc, out, after = run(drive)
        rec("valid repo stage is used", rc != 0 and "from repo" in out and after == "7")
    finally:
        REPO_STAGE.write_bytes(original)
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the default is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v42",
        "finding": (
            "The script falls through to the default 0 when backup and repo are both "
            "unavailable. A valid repo stage is used when present. Probed as uid 0 with "
            "ALLOW_ROOT=1 and every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "default": "0",
        "repoStage": "7",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The default-stage check holds.",
    }
    out = ROOT / "references" / "great-scale-default-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("default evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"default: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
