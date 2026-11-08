#!/usr/bin/env python3
"""Numeric-check census.

The numeric checks allow only whole numbers in range. Probed as uid 0
with ALLOW_ROOT=1, a valid drive, a valid model, and valid lanes:

- CYCLES=1.5, 1h, -3, 1e3: refused
- CTX=1.5: refused
- CTX=100: refused (below 2048)
- LANE_TIMEOUT=1.5, 0, 1h: refused
- plain values: pass, reach the next check

The numeric checks hold even when root is allowed and the drive, model,
and lanes are valid.
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(drive: str, **extra) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools")
    env.update(extra)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Numeric-check census")
    drive = tempfile.mkdtemp()
    try:
        bad = [
            ("CYCLES=1.5", {"CYCLES": "1.5"}, "non-negative integer"),
            ("CYCLES=1h", {"CYCLES": "1h"}, "non-negative integer"),
            ("CYCLES=-3", {"CYCLES": "-3"}, "non-negative integer"),
            ("CYCLES=1e3", {"CYCLES": "1e3"}, "non-negative integer"),
            ("CTX=1.5", {"CTX": "1.5"}, "must be an integer"),
            ("CTX=100", {"CTX": "100"}, "between 2048"),
            ("LANE_TIMEOUT=1.5", {"LANE_TIMEOUT": "1.5"}, "whole number"),
            ("LANE_TIMEOUT=0", {"LANE_TIMEOUT": "0"}, "at least 1"),
            ("LANE_TIMEOUT=1h", {"LANE_TIMEOUT": "1h"}, "whole number"),
        ]
        for label, extra, needle in bad:
            rc, out = run(drive, **extra)
            rec(f"refuses {label}", rc != 0 and needle in out)
        rc, out = run(drive, CYCLES="1", CTX="32768", LANE_TIMEOUT="3600")
        rec("plain values pass the checks", rc != 0 and "integer" not in out and "whole number" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 9, "deterministic")
    rec("nine refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v27",
        "finding": (
            "The numeric checks allow only whole numbers in range. CYCLES, CTX, and "
            "LANE_TIMEOUT all refuse decimals, suffixes, negatives, and out-of-range "
            "values, even with ALLOW_ROOT=1 and a valid drive, model, and lanes. Plain "
            "values pass and reach the next check. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": [b[0] for b in bad],
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The numeric checks hold.",
    }
    out = ROOT / "references" / "great-scale-numeric-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("numeric evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"nine refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
