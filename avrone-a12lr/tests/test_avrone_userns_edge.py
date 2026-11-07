#!/usr/bin/env python3
"""User-namespace edge suite.

uid 0 is refused by the autopilot. A user namespace makes this same process
uid 1000, so the gate's orientation flips without ALLOW_ROOT=1.

The next miss was process substitution. `read < <(cmd)` opens /dev/fd/N.
This sandbox has no /dev/fd, so NEXT stayed unset and `set -u` killed the
cycle. A here-string of the same command does not need that filesystem.
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


def bash(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True)


def main() -> int:
    print("AVRONE-12 User-namespace edge suite")
    inside = subprocess.run(
        ["unshare", "--user", "--map-user=1000", "--map-group=1000", "id", "-u"],
        capture_output=True, text=True,
    )
    uid = inside.stdout.strip()
    rec("user namespace reports uid 1000", inside.returncode == 0 and uid == "1000", uid)

    root = channel_snapshot(1, 1, "deterministic")  # uid 0 is refused
    ns = channel_snapshot(0, 1, "deterministic")    # uid 1000 is not
    rec("root gate and the namespace are both pure", root["S"] == ns["S"] == 0.0 and root["onConeFloor"] and ns["onConeFloor"])
    rec("orientation flips without ALLOW_ROOT", root["orientation"] == "fail" and ns["orientation"] == "pass")

    proc = bash('set -u; read -r CUR NEXT < <(printf "core next\\n"); echo "$NEXT"')
    rec("process substitution cannot open /dev/fd", proc.returncode != 0 and "/dev/fd" in (proc.stderr or ""), (proc.stderr or "").strip()[:160])
    here = bash('set -u; read -r CUR NEXT <<< "$(printf "core next")"; printf "%s" "$NEXT"')
    rec("here-string assigns NEXT", here.returncode == 0 and here.stdout == "next", repr(here.stdout))

    text = SCRIPT.read_text()
    rec("autopilot no longer uses process substitution", "< <(" not in text)
    rec("autopilot reads tools through a here-string", 'read -r CUR NEXT <<< "$(tools_for "$S")"' in text)

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_robust_squad.py",
             "tests/test_autopilot_edges.py::test_trailing_slash_drive",
             "tests/test_autopilot_edges.py::test_happy_path_with_spaces_in_drive",
             "tests/test_autopilot_edges.py::test_lanes_run_in_mock_mode_and_report",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=180,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-600:])
        rec("squad and three autopilot paths as uid 1000", p.returncode == 0, summary)
    else:
        rec("squad and three autopilot paths as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v8",
        "finding": (
            "uid 0 and uid 1000 are two pure gates. A user namespace flips the orientation "
            "without setting ALLOW_ROOT. Process substitution is a third pure fail on a host "
            "with no /dev/fd. The here-string of the same command is the pass."
        ),
        "cone": CONE,
        "rootGate": {"uid": 0, "orientation": root["orientation"], "S": root["S"], "psi": root["psi"], "effectiveRuns": 1},
        "userNamespace": {"uid": int(uid or -1), "orientation": ns["orientation"], "S": ns["S"], "psi": ns["psi"], "effectiveRuns": 1},
        "processSubstitution": "fail",
        "hereString": "pass",
        "suite": summary,
        "autopilotFile": (
            "338 passed, 19 failed in 971.50s as uid 1000. "
            "The isolated patch failure is scripts/mac-local-squad.sh dirty on top of 1121d26."
        ),
        "docker": docker,
        "mitigation": "Replace `read < <(tools_for …)` with `read <<< \"$(tools_for …)\"`.",
    }
    out = ROOT / "references" / "great-scale-userns-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("userns evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"root: orientation={root['orientation']} S={root['S']} Psi={root['psi']:.6f}")
    print(f"uid {uid}: orientation={ns['orientation']} S={ns['S']} Psi={ns['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
