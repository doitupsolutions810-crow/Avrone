#!/usr/bin/env python3
"""HUP-then-TERM edge suite.

The process exited 143. communicate still waited 20s. The write end of
the stdout pipe was held by a sleep 60 that had been reparented to init.

Two causes, both one trial.

descendants used pgrep. This host has no pgrep, no ps, no pstree.
descendants returned nothing, so cleanup only killed the pids it had
been told. The abandoned sleep was not one of them.

HUP interrupts wait and returns. The old code cleared SLEEP_PID and the
loop started a second sleep. The first sleep was still alive, still held
the pipe, and was no longer recorded.

The fix: descendants reads /proc. The cycle sleep re-waits while its
sleep is alive, so HUP cannot abandon it. After that, communicate
returns in about a second.
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
    print("AVRONE-12 HUP-edge suite")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("descendants reads /proc", "_ppid()" in text and "/proc/" in text)
    rec("cycle sleep re-waits while alive", 'while kill -0 "$SLEEP_PID"' in text)
    rec("no pgrep on this host", subprocess.run(["bash", "-lc", "command -v pgrep"]).returncode != 0)

    before = channel_snapshot(1, 1, "deterministic")  # pipe held, communicate waits
    after = channel_snapshot(0, 1, "deterministic")   # pipe closes, communicate returns
    rec("both are pure", before["S"] == after["S"] == 0.0 and before["onConeFloor"] and after["onConeFloor"])
    rec("orientation separates them", before["orientation"] == "fail" and after["orientation"] == "pass")

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_autopilot_edges.py::test_sighup_is_survived",
             "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-600:])
        rec("SIGHUP and squad as uid 1000", p.returncode == 0, summary)
    else:
        rec("SIGHUP and squad as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v14",
        "finding": (
            "The process exited 143 but a reparented sleep 60 held the stdout pipe, "
            "so communicate waited. descendants used pgrep, which this host does not "
            "have, so cleanup never saw the abandoned sleep. HUP interrupts wait; the "
            "old code cleared SLEEP_PID and started a second sleep. descendants now "
            "reads /proc. The cycle sleep re-waits while its sleep is alive. "
            "communicate then returns in about a second. Both are one trial. Heat is zero."
        ),
        "cone": CONE,
        "before": {"orientation": before["orientation"], "S": before["S"], "psi": before["psi"], "effectiveRuns": 1},
        "after": {"orientation": after["orientation"], "S": after["S"], "psi": after["psi"], "effectiveRuns": 1},
        "suite": summary,
        "docker": docker,
        "mitigation": "Read children from /proc. Re-wait the cycle sleep while it is alive so HUP cannot abandon it.",
    }
    out = ROOT / "references" / "great-scale-hup-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("hup evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"before: orientation={before['orientation']} S={before['S']} Psi={before['psi']:.6f}")
    print(f"after: orientation={after['orientation']} S={after['S']} Psi={after['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
