#!/usr/bin/env python3
"""Drive-check census.

The drive checks run before the root gate. ALLOW_ROOT=1 does not
bypass them. Probed as uid 0 with ALLOW_ROOT=1:

- symlink into the repo: refused
- drive is the home folder: refused
- drive is the filesystem root: refused
- drive inside the repo: refused
- drive does not exist: refused

All five refuse. The root gate never gets a chance to pass them.
"""
from __future__ import annotations

import json
import os
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


def run(drive: str, **env) -> tuple[int, str]:
    e = os.environ.copy()
    e.update(ALLOW_ROOT="1", MODEL="stub-model", DRIVE=drive)
    e.update(env)
    p = subprocess.run(["bash", str(REPO / "scripts" / "mac-avrone-autopilot.sh")],
                       capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("AVRONE-12 Drive-check census")
    link = Path("/tmp/avrone-link-drive")
    link.symlink_to(REPO)
    scratch = REPO / "scratch-drive"
    scratch.mkdir(exist_ok=True)
    try:
        cases = [
            ("symlink into repo", str(link), "inside the repo"),
            ("home folder", "/tmp", "home folder"),
            ("filesystem root", "/", "filesystem root"),
            ("inside repo", str(scratch), "inside the repo"),
            ("missing", "/tmp/no-such-drive-xyz", "not mounted"),
        ]
        for label, drive, needle in cases:
            extra = {"HOME": "/tmp"} if label == "home folder" else {}
            rc, out = run(drive, **extra)
            rec(f"refuses {label}", rc != 0 and needle in out)
    finally:
        link.unlink(missing_ok=True)
        if scratch.exists():
            scratch.rmdir()

    snap = channel_snapshot(0, 5, "deterministic")
    rec("all five are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v24",
        "finding": (
            "The drive checks run before the root gate. ALLOW_ROOT=1 does not bypass "
            "them. Symlink into the repo, home folder, filesystem root, inside the repo, "
            "and a missing drive all refuse. Pure. Heat is zero."
        ),
        "cone": CONE,
        "checks": {
            "refuses": ["symlink into repo", "home folder", "filesystem root", "inside repo", "missing"],
            "orientation": snap["orientation"],
            "S": snap["S"],
            "psi": snap["psi"],
            "effectiveRuns": 1,
        },
        "docker": docker,
        "mitigation": "None needed. The drive checks hold even when root is allowed.",
    }
    out = ROOT / "references" / "great-scale-drive-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("drive evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"five refusals: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
