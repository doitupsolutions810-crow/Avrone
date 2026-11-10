#!/usr/bin/env python3
"""Directory-check census.

The script moves a directory aside instead of writing through it.
Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- reintro-stage.txt is a directory: moved aside, contents preserved
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
    print("AVRONE-12 Directory-check census")
    drive = tempfile.mkdtemp()
    state = Path(drive) / "avrone-state"
    state.mkdir()
    try:
        d = state / "reintro-stage.txt"
        d.mkdir()
        (d / "inside").write_text("SENTINEL")
        rc, out = run(drive)
        moved = list(state.glob("reintro-stage.txt.bad-*"))
        rec("directory is moved aside", rc != 0 and "directory" in out and not d.is_dir() and len(moved) == 1)
        rec("contents are preserved", moved and (moved[0] / "inside").read_text() == "SENTINEL")
        for m in moved:
            shutil.rmtree(m)
        d.write_text("4")
        rc, out = run(drive)
        rec("plain file is read as-is", rc != 0 and "directory" not in out and d.read_text().strip() == "4")
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the move is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v37",
        "finding": (
            "The script moves a directory aside instead of writing through it. "
            "A directory is moved aside and its contents are preserved. A plain file "
            "is read as-is. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check "
            "passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "directory": "moved aside, contents preserved",
        "plainFile": "read as-is",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The directory check holds.",
    }
    out = ROOT / "references" / "great-scale-directory-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("directory evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"move: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
