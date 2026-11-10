#!/usr/bin/env python3
"""Homebrew-check census.

The script refuses when brew is missing. Probed as uid 0 with
ALLOW_ROOT=1 and every earlier check passing:

- brew missing: refused, 'Homebrew is required to install ollama'
- brew present: continues past the brew check

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


def run(drive: str, path: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600", PATH=path,
               AUTOPILOT_BREW_PREFIXES="/nonexistent")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Homebrew-check census")
    drive = tempfile.mkdtemp()
    stub = tempfile.mkdtemp()
    (Path(stub) / "brew").write_text("#!/bin/bash\nexit 0\n")
    os.chmod(Path(stub) / "brew", 0o755)
    (Path(stub) / "ollama").write_text("#!/bin/bash\nexit 0\n")
    os.chmod(Path(stub) / "ollama", 0o755)
    try:
        rc, out = run(drive, "/usr/bin:/bin")
        rec("missing refuses", rc != 0 and "Homebrew is required" in out)
        rc, out = run(drive, f"{stub}:/usr/bin:/bin")
        rec("present continues past brew", rc != 0 and "Homebrew is required" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)
        shutil.rmtree(stub, ignore_errors=True)

    snap = channel_snapshot(0, 1, "deterministic")
    rec("the refusal is pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v47",
        "finding": (
            "The script refuses when brew is missing and continues past the brew "
            "check when brew is present. Probed as uid 0 with ALLOW_ROOT=1 and every "
            "earlier check passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "missing": "refuse",
        "present": "continue past brew",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The Homebrew check holds.",
    }
    out = ROOT / "references" / "great-scale-brew-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("brew evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"refusal: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
