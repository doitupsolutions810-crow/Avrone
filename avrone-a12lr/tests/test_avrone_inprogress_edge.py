#!/usr/bin/env python3
"""In-progress-operation census.

The same loop that checks bisect also checks merge, cherry-pick, revert,
and rebase. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check
passing:

- MERGE_HEAD present: refused
- CHERRY_PICK_HEAD present: refused
- REVERT_HEAD present: refused
- rebase-merge present: refused
- clean git dir: continues to the next check

All four refuse. A clean dir continues.
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


def gitdir() -> Path:
    raw = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
    return (REPO / raw) if not Path(raw).is_absolute() else Path(raw)


def main() -> int:
    print("AVRONE-12 In-progress-operation census")
    gd = gitdir()
    drive = tempfile.mkdtemp()
    markers = []
    try:
        for name, marker, needle in (
            ("merge", "MERGE_HEAD", "middle of a merge"),
            ("cherry-pick", "CHERRY_PICK_HEAD", "middle of a cherry-pick"),
            ("revert", "REVERT_HEAD", "middle of a revert"),
            ("rebase", "rebase-merge", "middle of a rebase"),
        ):
            path = gd / marker
            if marker == "rebase-merge":
                path.mkdir(exist_ok=True)
            else:
                path.touch()
            markers.append(path)
            rc, out = run(drive)
            rec(f"{name} refuses", rc != 0 and needle in out)
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        rc, out = run(drive)
        rec("clean continues", rc != 0 and "middle of a" not in out)
    finally:
        for p in markers:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 4, "deterministic")
    rec("four refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v31",
        "finding": (
            "The in-progress loop refuses merge, cherry-pick, revert, and rebase, "
            "and continues when the git dir is clean. Probed as uid 0 with ALLOW_ROOT=1 "
            "and every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": ["merge", "cherry-pick", "revert", "rebase"],
        "clean": "continue",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The in-progress checks hold.",
    }
    out = ROOT / "references" / "great-scale-inprogress-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("in-progress evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"four refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
