#!/usr/bin/env python3
"""Unshallow edge suite.

The autopilot suite was skipped while the clone was shallow (base 1121d26
missing). After unshallow, the suite was silent: /dev/fd is not mounted here,
and the closed-fd probe treated that as a closed stdout and discarded every
FATAL. The probe now uses /proc/self/fd when /dev/fd is absent.

The next gate is deterministic too. uid 0 is refused unless ALLOW_ROOT=1.
This sandbox cannot drop uid, so the rest of the autopilot suite does not run.
That is one trial, heat zero, orientation fail. Not a flake.
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


def run_script(extra_env: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("DRIVE", None)
    env.pop("ALLOW_ROOT", None)
    env.update(extra_env)
    return subprocess.run(["bash", str(SCRIPT)], cwd=REPO, env=env, capture_output=True, text=True, timeout=20)


def main() -> int:
    print("AVRONE-12 Unshallow-edge suite")
    shallow = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True)
    rec("clone is not shallow", shallow.stdout.strip() == "false", shallow.stdout.strip())
    base = subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", "1121d26"], capture_output=True, text=True)
    rec("base commit 1121d26 is present", base.returncode == 0 and base.stdout.strip() == "commit")

    rec("/dev/fd is absent here", not Path("/dev/fd").exists())
    rec("/proc/self/fd is present", Path("/proc/self/fd").is_dir())
    text = SCRIPT.read_text()
    rec("probe falls back to proc", "_fd_ok" in text and "/proc/self/fd" in text)
    rec("probe does not use bare /dev/fd existence as closed", "[ -e /dev/fd/1 ] || exec 1>/dev/null" not in text)

    no_drive = run_script({})
    blob = no_drive.stdout + no_drive.stderr
    rec("missing DRIVE is visible", no_drive.returncode == 1 and "set DRIVE" in blob, blob.strip()[:180])

    root = run_script({"DRIVE": "/tmp"})
    rblob = root.stdout + root.stderr
    uid = os.geteuid()
    if uid == 0:
        rec("root is refused with a visible FATAL", root.returncode == 1 and "ALLOW_ROOT" in rblob, rblob.strip()[:180])
        root_snap = channel_snapshot(1, 1, "deterministic")
    else:
        rec("non-root is not stopped by the root gate", "ALLOW_ROOT" not in rblob)
        root_snap = channel_snapshot(0, 1, "deterministic")
    rec("root gate is one trial on the cone floor", root_snap["effectiveRuns"] == 1 and root_snap["S"] == 0.0 and root_snap["onConeFloor"])

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py",
             "tests/test_autopilot_edges.py::test_no_drive_fails_cleanly",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-800:])
        rec("squad and no-drive autopilot test", p.returncode == 0, summary)
    else:
        rec("squad and no-drive autopilot test", False, "venv missing")

    payload = {
        "schema": "great-scale-evidence/v7",
        "finding": (
            "A missing /dev/fd directory is not a closed stdout. Treating it as one "
            "silenced every autopilot failure. After the probe can see the descriptor, "
            "uid 0 is a second pure gate: the suite stops, heat is zero, orientation is fail."
        ),
        "cone": CONE,
        "shallow": False,
        "baseCommit": "1121d26",
        "devFdMounted": Path("/dev/fd").exists(),
        "procFdMounted": Path("/proc/self/fd").is_dir(),
        "rootGate": {
            "uid": uid,
            "orientation": root_snap["orientation"],
            "S": root_snap["S"],
            "psi": root_snap["psi"],
            "effectiveRuns": root_snap["effectiveRuns"],
            "onConeFloor": root_snap["onConeFloor"],
        },
        "suite": summary,
        "mitigation": (
            "Probe /dev/fd only when that directory exists; otherwise use /proc/self/fd. "
            "If neither exists, do not redirect a live descriptor to /dev/null."
        ),
    }
    out = ROOT / "references" / "great-scale-unshallow-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("unshallow evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(
        f"root gate uid={uid}: orientation={root_snap['orientation']} "
        f"S={root_snap['S']} Psi={root_snap['psi']:.6f} effectiveRuns={root_snap['effectiveRuns']}"
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
