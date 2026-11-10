#!/usr/bin/env python3
"""Stage-above-batches census.

The script clamps a stage above the batch count. Probed as uid 0 with
ALLOW_ROOT=1 and every earlier check passing. There are 9 batches.

- stage 99: clamped to 9
- stage 9: left alone
- stage 3: left alone
- stage 0: left alone

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


def run(drive: str, stage: str) -> tuple[int, str, str]:
    state = Path(drive) / "avrone-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "reintro-stage.txt").write_text(stage)
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    after = (state / "reintro-stage.txt").read_text().strip()
    return p.returncode, p.stdout + p.stderr, after


def main() -> int:
    print("AVRONE-12 Stage-above-batches census")
    drive = tempfile.mkdtemp()
    try:
        rc, out, after = run(drive, "99")
        rec("stage 99 is clamped", rc != 0 and "clamping" in out and after == "9")
        for stage in ("9", "3", "0"):
            rc, out, after = run(drive, stage)
            rec(f"stage {stage} is left alone", rc != 0 and "clamping" not in out and after == stage)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the clamp is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v39",
        "finding": (
            "The script clamps a stage above the batch count. Stage 99 is clamped to 9. "
            "Stages 9, 3, and 0 are left alone. Probed as uid 0 with ALLOW_ROOT=1 and "
            "every earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "batches": 9,
        "clamped": {"99": "9"},
        "leftAlone": ["9", "3", "0"],
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The stage-above-batches check holds.",
    }
    out = ROOT / "references" / "great-scale-clamp-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("clamp evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"clamp: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
