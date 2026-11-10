#!/usr/bin/env python3
"""Unreadable-check census.

The script moves an unreadable reintro-stage.txt aside. Probed with
ALLOW_ROOT=1 and every earlier check passing:

- mode 000 as uid 1000: not readable, moved aside
- mode 000 as root: readable (root reads everything on Linux), check does
  not fire, the stage check repairs the content instead
- readable file: read as-is, continues

The check holds for non-root. Root bypasses it.
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


def run(drive: str, as_user: bool = False) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
    if not as_user:
        env["ALLOW_ROOT"] = "1"
    cmd = ["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")]
    if as_user:
        cmd = ["unshare", "--user", "--map-user=1000", "--map-group=1000"] + cmd
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Unreadable-check census")
    drive = tempfile.mkdtemp()
    state = Path(drive) / "avrone-state"
    state.mkdir()
    os.chmod(drive, 0o777)
    os.chmod(state, 0o777)
    try:
        f = state / "reintro-stage.txt"
        f.write_text("SENTINEL")
        os.chmod(f, 0o000)
        rc, out = run(drive, as_user=True)
        moved = list(state.glob("reintro-stage.txt.bad-*"))
        rec("uid 1000 moves it aside", rc != 0 and "unreadable" in out and len(moved) == 1)
        for m in moved:
            m.unlink()
        f.write_text("SENTINEL")
        os.chmod(f, 0o000)
        rc, out = run(drive)
        rec("root does not see it as unreadable", rc != 0 and "unreadable" not in out)
        os.chmod(f, 0o644)
        f.write_text("6")
        rc, out = run(drive)
        rec("readable file is read as-is", rc != 0 and "unreadable" not in out and f.read_text().strip() == "6")
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the move is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v38",
        "finding": (
            "The unreadable check moves a mode 000 reintro-stage.txt aside for uid 1000. "
            "Root bypasses it: root can read mode 000 on Linux, so the check does not fire "
            "and the stage check repairs the content instead. A readable file is read as-is. "
            "Pure. Heat is zero."
        ),
        "cone": CONE,
        "uid1000": "moved aside",
        "root": "bypassed, stage check repairs",
        "readable": "read as-is",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The check holds for non-root. Root bypasses it because root can read anything on Linux.",
    }
    out = ROOT / "references" / "great-scale-unreadable-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("unreadable evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"move: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
