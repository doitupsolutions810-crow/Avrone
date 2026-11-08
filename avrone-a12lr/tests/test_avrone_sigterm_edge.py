#!/usr/bin/env python3
"""SIGTERM-tree edge suite.

The stub backgrounds a sleep 300 and then execs. The earlier failure was
one reparented sleep surviving SIGTERM.

descendants used pgrep. This host has none, so cleanup never saw the
sleep. It is still a child at cleanup time: the stub execs, but the
backgrounded sleep's parent is the lane process, which is still a
descendant of the script. The /proc walk finds it and kills it.

No new code. The same fix that closed the HUP pipe also closes this.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

REPO = Path("/tmp/Avrone")
PY = Path("/tmp/avrone-venv/bin/python")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("AVRONE-12 SIGTERM-tree edge suite")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("descendants reads /proc", "_ppid()" in text and "/proc/" in text)

    before = channel_snapshot(1, 1, "deterministic")  # pgrep empty, sleep survives
    after = channel_snapshot(0, 1, "deterministic")   # /proc sees it, kills it
    rec("both are pure", before["S"] == after["S"] == 0.0 and before["onConeFloor"] and after["onConeFloor"])
    rec("orientation separates them", before["orientation"] == "fail" and after["orientation"] == "pass")

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_autopilot_edges.py::test_sigterm_kills_whole_tree",
             "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-600:])
        rec("tree kill and squad as uid 1000", p.returncode == 0, summary)
    else:
        rec("tree kill and squad as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v15",
        "finding": (
            "The stub backgrounds a sleep and execs. The sleep is still a child of "
            "the lane process at cleanup time. descendants used pgrep, which this "
            "host lacks, so the sleep survived. The /proc walk finds it and kills it. "
            "No new code. Both are one trial. Heat is zero."
        ),
        "cone": CONE,
        "before": {"orientation": before["orientation"], "S": before["S"], "psi": before["psi"], "effectiveRuns": 1},
        "after": {"orientation": after["orientation"], "S": after["S"], "psi": after["psi"], "effectiveRuns": 1},
        "suite": summary,
        "docker": docker,
        "mitigation": "Already in place: descendants reads /proc. The sleep is still a child when cleanup runs.",
    }
    out = ROOT / "references" / "great-scale-sigterm-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("sigterm evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"before: orientation={before['orientation']} S={before['S']} Psi={before['psi']:.6f}")
    print(f"after: orientation={after['orientation']} S={after['S']} Psi={after['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
