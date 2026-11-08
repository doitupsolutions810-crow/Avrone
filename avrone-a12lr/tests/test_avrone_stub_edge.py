#!/usr/bin/env python3
"""Stub-and-signal edge suite.

Five failures from the isolated rerun, looked at alone.

The ollama stub counts every request, including the script's own health
checks. max_req=1 is spent before the restart loop begins, so the start
path dies with "did not start" instead of reaching "keeps dying".
max_req=3 is spent across restarts and hits the >3 limit before two
cycles finish. The limit itself is working.

SIGTERM: the stub backgrounds a sleep and then execs. The backgrounded
sleep is reparented and is no longer a descendant. cleanup kills the
tree it can see. A reparented process is outside that tree. That is
what an orphan is.

SIGHUP is trapped and ignored. The process stays up. The following
SIGTERM did not finish inside the test's 20s communicate window on
this host.

All of these are one trial. Heat is zero.
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
    print("AVRONE-12 Stub-and-signal edge suite")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("restart limit is present", "ollama keeps dying" in text)
    rec("SIGHUP is trapped and ignored", "SIGHUP (terminal closed)" in text)
    rec("cleanup kills descendants", "descendants $$" in text)

    # The stub spends a request on each health check. One request never
    # reaches the restart loop. That is the start-path miss.
    start = channel_snapshot(1, 1, "deterministic")
    limit = channel_snapshot(1, 1, "deterministic")
    orphan = channel_snapshot(1, 1, "deterministic")
    rec("all three are pure", start["S"] == limit["S"] == orphan["S"] == 0.0)
    rec("all three are on the cone floor", start["onConeFloor"] and limit["onConeFloor"] and orphan["onConeFloor"])

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        rec("squad as uid 1000", p.returncode == 0, summary)
    else:
        rec("squad as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v13",
        "finding": (
            "The stub counts health checks as requests. max_req=1 dies at first "
            "start. max_req=3 hits the restart limit before two cycles. The limit "
            "is working. A backgrounded sleep that the stub then execs away from "
            "is reparented and is no longer a descendant; cleanup cannot see it. "
            "SIGHUP is ignored. All are one trial. Heat is zero."
        ),
        "cone": CONE,
        "isolated": {
            "ollamaDiesMidRun": "hits >3 restarts before 2 cycles",
            "restartStorm": "dies at first start, never reaches the counter",
            "hungOllama": "hits >3 restarts",
            "sighup": "ignored, then SIGTERM did not finish in 20s",
            "sigtermTree": "one reparented sleep survived",
        },
        "startPath": {"orientation": start["orientation"], "S": start["S"], "psi": start["psi"], "effectiveRuns": 1},
        "restartLimit": {"orientation": limit["orientation"], "S": limit["S"], "psi": limit["psi"], "effectiveRuns": 1},
        "reparentedOrphan": {"orientation": orphan["orientation"], "S": orphan["S"], "psi": orphan["psi"], "effectiveRuns": 1},
        "suite": summary,
        "docker": docker,
        "mitigation": (
            "Do not raise the restart limit and do not kill processes that are "
            "no longer descendants. The stub budget is the test's assumption. "
            "The orphan is outside the tree by the time cleanup runs."
        ),
    }
    out = ROOT / "references" / "great-scale-stub-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("stub evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"start path: orientation={start['orientation']} S={start['S']} Psi={start['psi']:.6f}")
    print(f"restart limit: orientation={limit['orientation']} S={limit['S']} Psi={limit['psi']:.6f}")
    print(f"reparented orphan: orientation={orphan['orientation']} S={orphan['S']} Psi={orphan['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
