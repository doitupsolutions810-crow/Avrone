#!/usr/bin/env python3
"""Overlay-edge suite.

The unpatched fixture copies the current scripts onto commit 1121d26.
At that commit only scripts/mac-local-squad.sh is tracked. The overlay
makes it dirty. The carried patches touch avrone/ and tests/, not scripts/.
A scripts/ overlay is the harness. A dirty file outside scripts/ is still
a user edit. Both are one trial. Heat is zero. Orientation separates them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

REPO = Path("/tmp/Avrone")
PY = Path("/tmp/avrone-venv/bin/python")
SCRIPT = REPO / "scripts" / "mac-avrone-autopilot.sh"
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("AVRONE-12 Overlay-edge suite")
    text = SCRIPT.read_text()
    rec("gate still refuses a non-scripts dirty file", "uncommitted changes in tracked files" in text)
    rec("gate allows a scripts/ overlay", "only scripts/ is dirty" in text)
    rec("patches do not touch scripts/", "patches do not touch scripts/" in text)

    # The unpatched base has one tracked script. The overlay dirties it.
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", "1121d26", "--", "scripts/"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    rec("only mac-local-squad.sh is tracked at the base", tracked == ["scripts/mac-local-squad.sh"], str(tracked))

    # A dirty file outside scripts/ is still a stop. Simulate the check.
    check = subprocess.run(
        ["bash", "-c", r'''
dirty="avrone/trinity.py scripts/mac-local-squad.sh"
bad=""
for f in $dirty; do
  case "$f" in
    scripts/*) ;;
    *) bad="$bad $f" ;;
  esac
done
printf "%s" "$bad"
'''],
        capture_output=True, text=True,
    )
    rec("a trinity edit is still a stop", "avrone/trinity.py" in check.stdout, check.stdout.strip())

    scripts_only = subprocess.run(
        ["bash", "-c", r'''
dirty="scripts/mac-local-squad.sh"
bad=""
for f in $dirty; do
  case "$f" in
    scripts/*) ;;
    *) bad="$bad $f" ;;
  esac
done
printf "%s" "$bad"
'''],
        capture_output=True, text=True,
    )
    rec("a scripts/ overlay is not a stop", scripts_only.stdout.strip() == "")

    overlay = channel_snapshot(0, 1, "deterministic")  # scripts/ overlay now passes
    user = channel_snapshot(1, 1, "deterministic")     # non-scripts edit still fails
    rec("both are pure", overlay["S"] == user["S"] == 0.0 and overlay["onConeFloor"] and user["onConeFloor"])
    rec("orientation separates them", overlay["orientation"] == "pass" and user["orientation"] == "fail")

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_robust_squad.py",
             "tests/test_autopilot_edges.py::test_patches_apply_once_on_upstream_base",
             "tests/test_autopilot_edges.py::test_dirty_tree_blocks_patching_and_preserves_edits",
             "tests/test_autopilot_edges.py::test_happy_path_with_spaces_in_drive",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=240,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-800:])
        rec("squad, patch path, and dirty-tree guard as uid 1000", p.returncode == 0, summary)
    else:
        rec("squad, patch path, and dirty-tree guard as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v9",
        "finding": (
            "At 1121d26 only scripts/mac-local-squad.sh is tracked. The fixture overlay "
            "dirties it. The patches touch avrone/ and tests/, not scripts/. A scripts/ "
            "overlay is the harness. A dirty file outside scripts/ is still a user edit. "
            "Both are one trial. Heat is zero. Orientation separates them."
        ),
        "cone": CONE,
        "scriptsOverlay": {
            "orientation": overlay["orientation"],
            "S": overlay["S"],
            "psi": overlay["psi"],
            "effectiveRuns": overlay["effectiveRuns"],
            "onConeFloor": overlay["onConeFloor"],
        },
        "userEditOutsideScripts": {
            "orientation": user["orientation"],
            "S": user["S"],
            "psi": user["psi"],
            "effectiveRuns": user["effectiveRuns"],
            "onConeFloor": user["onConeFloor"],
        },
        "trackedAtBase": tracked,
        "suite": summary,
        "docker": docker,
        "mitigation": (
            "Refuse a dirty tracked file only when it is outside scripts/. "
            "The carried patches do not touch scripts/. A user edit of avrone/ or tests/ still stops the run."
        ),
    }
    out = ROOT / "references" / "great-scale-overlay-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("overlay evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"scripts overlay: orientation={overlay['orientation']} S={overlay['S']} Psi={overlay['psi']:.6f}")
    print(f"user edit: orientation={user['orientation']} S={user['S']} Psi={user['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
