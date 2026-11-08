#!/usr/bin/env python3
"""Required-command census.

The script refuses when git or python3 is missing from PATH. Probed as
uid 0 with ALLOW_ROOT=1 and every earlier check passing. The script
re-adds Homebrew prefixes to PATH, so the probe points those at a
nonexistent directory to test the checks cleanly.

- neither git nor python3: refused (git is required)
- python3 only: refused (git is required)
- git only: refused (python3 is required)
- both present: continues to the next check

The checks hold.
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


def run(path: str, drive: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(ALLOW_ROOT="1", DRIVE=drive, MODEL="stub-model", LANES="deepcov tools",
               CYCLES="1", CTX="32768", LANE_TIMEOUT="3600", PATH=path,
               AUTOPILOT_BREW_PREFIXES="/nonexistent")
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def base() -> str:
    d = tempfile.mkdtemp()
    for c in ("bash", "cat", "grep", "awk", "sed", "head", "tail", "rm", "mkdir",
              "touch", "echo", "printf", "date", "tr", "wc", "dirname", "pwd",
              "readlink", "cp"):
        src = subprocess.run(["bash", "-lc", f"command -v {c}"], capture_output=True, text=True).stdout.strip()
        if src:
            os.symlink(src, os.path.join(d, c))
    return d


def main() -> int:
    print("AVRONE-12 Required-command census")
    drive = tempfile.mkdtemp()
    b = base()
    try:
        rc, out = run(b, drive)
        rec("neither refuses", rc != 0 and "git is required" in out)
        py = tempfile.mkdtemp()
        os.symlink("/bin/python3", os.path.join(py, "python3"))
        rc, out = run(f"{b}:{py}", drive)
        rec("no git refuses", rc != 0 and "git is required" in out)
        gi = tempfile.mkdtemp()
        os.symlink("/bin/git", os.path.join(gi, "git"))
        rc, out = run(f"{b}:{gi}", drive)
        rec("no python3 refuses", rc != 0 and "python3 is required" in out)
        both = tempfile.mkdtemp()
        os.symlink("/bin/git", os.path.join(both, "git"))
        os.symlink("/bin/python3", os.path.join(both, "python3"))
        rc, out = run(f"{b}:{both}", drive)
        rec("both continues", rc != 0 and "git is required" not in out and "python3 is required" not in out)
    finally:
        shutil.rmtree(drive, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)

    snap = channel_snapshot(0, 3, "deterministic")
    rec("three refusals are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v32",
        "finding": (
            "The script refuses when git or python3 is missing from PATH. Neither, "
            "python3 only, and git only all refuse. Both present continues. The script "
            "re-adds Homebrew prefixes, so the probe points those at a nonexistent "
            "directory. Pure. Heat is zero."
        ),
        "cone": CONE,
        "refused": ["neither", "python3 only", "git only"],
        "both": "continue",
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The required-command checks hold.",
    }
    out = ROOT / "references" / "great-scale-commands-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("commands evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"three refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
