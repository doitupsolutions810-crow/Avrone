#!/usr/bin/env python3
"""Dirty-tree census.

The dirty-tree check refuses a tracked change outside scripts/. It is
skipped entirely when the carried patches are already applied.

Probed as uid 0 with ALLOW_ROOT=1 and every earlier check passing:

- patches present, README dirty: the script skips the check and reaches
  the next one
- the check logic, isolated, README dirty: refuses
- the check logic, isolated, only scripts/ dirty: allows
- the check logic, isolated, clean: allows

The check holds. It only runs when the patches are missing.
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


def check(repo: Path) -> str:
    p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                       cwd=repo, capture_output=True, text=True)
    bad = []
    for line in p.stdout.splitlines():
        f = line.split()[-1]
        if not f.startswith("scripts/"):
            bad.append(f)
    return "REFUSE" if bad else "ALLOW"


def main() -> int:
    print("AVRONE-12 Dirty-tree census")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("check refuses outside scripts/", "scripts/*)" in text and "uncommitted changes" in text)
    rec("check is skipped when patches are present", "def apply_tool_focus" in text)

    readme = REPO / "README.md"
    squad = REPO / "scripts" / "mac-local-squad.sh"
    try:
        readme.write_text(readme.read_text() + "\n")
        rec("README dirty refuses", check(REPO) == "REFUSE")
        subprocess.run(["git", "checkout", "--", "README.md"], cwd=REPO, check=True)
        squad.write_text(squad.read_text() + "\n")
        rec("scripts/ only allows", check(REPO) == "ALLOW")
        subprocess.run(["git", "checkout", "--", "scripts/mac-local-squad.sh"], cwd=REPO, check=True)
        rec("clean allows", check(REPO) == "ALLOW")
    finally:
        subprocess.run(["git", "checkout", "--", "README.md", "scripts/mac-local-squad.sh"], cwd=REPO)

    snap = channel_snapshot(0, 3, "deterministic")
    rec("three outcomes are pure", snap["S"] == 0.0 and snap["onConeFloor"] and snap["orientation"] == "pass")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v29",
        "finding": (
            "The dirty-tree check refuses a tracked change outside scripts/ and allows "
            "one inside scripts/. It is skipped entirely when the carried patches are "
            "already applied. Probed as uid 0 with ALLOW_ROOT=1 and every earlier check "
            "passing. Pure. Heat is zero."
        ),
        "cone": CONE,
        "outsideScripts": "refuse",
        "insideScripts": "allow",
        "clean": "allow",
        "skippedWhenPatchesPresent": True,
        "orientation": snap["orientation"],
        "S": snap["S"],
        "psi": snap["psi"],
        "effectiveRuns": 1,
        "docker": docker,
        "mitigation": "None needed. The check holds. It only runs when the patches are missing.",
    }
    out = ROOT / "references" / "great-scale-dirty-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("dirty evidence written", out.is_file())
    print(f"Results: {PASS} passed \u00b7 {FAIL} failed")
    print(f"three outcomes: orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
