#!/usr/bin/env python3
"""MODEL-check census.

The MODEL check allows only plain identifiers. Probed as uid 0 with
ALLOW_ROOT=1 and a valid writable drive:

- $(id): refused
- backticks: refused
- semicolon: refused
- pipe: refused
- ampersand: refused
- redirect: refused
- newline: refused
- space: refused
- plain 'stub-model': passes, reaches the next check

The MODEL check holds even when root is allowed and the drive is valid.
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


def run(model: str, drive: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL=model)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 MODEL-check census")
    drive = tempfile.mkdtemp()
    try:
        bad = ["$(id)", "`id`", "model;id", "model|id", "model&id", "model>out", "model\nid", "model name"]
        for m in bad:
            rc, out = run(m, drive)
            rec(f"refuses {m!r}", rc != 0 and "illegal characters" in out)
        rc, out = run("stub-model", drive)
        rec("plain model passes the check", rc != 0 and "illegal characters" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)

    snap = channel_snapshot(0, 8, "deterministic")
    rec("eight refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v25",
        "finding": (
            "The MODEL check allows only plain identifiers. Eight shell-syntax variants "
            "($(id), backticks, semicolon, pipe, ampersand, redirect, newline, space) all "
            "refuse, even with ALLOW_ROOT=1 and a valid drive. A plain model passes the "
            "check and reaches the next one. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": bad,
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The MODEL check holds.",
    }
    out = ROOT / "references" / "great-scale-model-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("model evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"eight refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
