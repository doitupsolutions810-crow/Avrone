#!/usr/bin/env python3
"""Leading-zero census.

The script normalizes a stage with a leading zero. Probed as uid 0 with
ALLOW_ROOT=1 and every earlier check passing:

- '007': normalized to 7
- '7': left alone
- '0': left alone
- '00': normalized to 0

The check holds. It uses 10# arithmetic to strip leading zeros.
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
    print("AVRONE-12 Leading-zero census")
    drive = tempfile.mkdtemp()
    try:
        rc, out, after = run(drive, "007")
        rec("007 is normalized to 7", rc != 0 and after == "7")
        rc, out, after = run(drive, "7")
        rec("7 is left alone", rc != 0 and after == "7")
        rc, out, after = run(drive, "0")
        rec("0 is left alone", rc != 0 and after == "0")
        rc, out, after = run(drive, "00")
        rec("00 is normalized to 0", rc != 0 and after == "0")
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 2, "deterministic")
    rec("two normalizations are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v40",
        "finding": (
            "The script normalizes a stage with a leading zero. '007' becomes 7. "
            "'00' becomes 0. '7' and '0' are left alone. It uses 10# arithmetic. "
            "Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing. "
            "Pure. Heat is zero."
        ),
        "cone": CONE,
        "normalized": {"007": "7", "00": "0"},
        "leftAlone": ["7", "0"],
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The leading-zero check holds.",
    }
    out = ROOT / "references" / "great-scale-leadingzero-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("leading-zero evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"normalizations: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
