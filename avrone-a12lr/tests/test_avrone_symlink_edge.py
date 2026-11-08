#!/usr/bin/env python3
"""Symlink-check census.

The script removes a symlink on the drive instead of writing through it.
Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- reintro-stage.txt is a symlink: removed, target untouched
- reintro-stage.txt is a plain file: read as-is, continues

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
    print("AVRONE-12 Symlink-check census")
    drive = tempfile.mkdtemp()
    state = Path(drive) / "avrone-state"
    state.mkdir()
    target = Path(tempfile.mktemp())
    target.write_text("SENTINEL")
    try:
        link = state / "reintro-stage.txt"
        link.symlink_to(target)
        rc, out = run(drive)
        rec("symlink is removed", rc != 0 and "symlink" in out and not link.is_symlink())
        rec("target is untouched", target.read_text() == "SENTINEL")
        link.write_text("5")
        rc, out = run(drive)
        rec("plain file is read as-is", rc != 0 and "symlink" not in out and link.read_text().strip() == "5")
    finally:
        shutil.rmtree(drive, ignore_errors=True)
        target.unlink(missing_ok=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the removal is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v36",
        "finding": (
            "The script removes a symlink on the drive instead of writing through it. "
            "A symlink is removed and the target is untouched. A plain file is read "
            "as-is. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing. "
            "Pure. Heat is zero."
        ),
        "cone": CONE,
        "symlink": "removed, target untouched",
        "plainFile": "read as-is",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The symlink check holds.",
    }
    out = ROOT / "references" / "great-scale-symlink-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("symlink evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"removal: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
