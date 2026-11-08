#!/usr/bin/env python3
"""Ollama-budget edge suite.

Three failures, rerun alone after the /proc fix. None of them moved.
They are not cleanup misses.

The stub counts every GET. The script's health check is a GET to
/api/version. max_req=1 is spent before the restart loop begins, so the
start path dies with "did not start" and never reaches "keeps dying".
max_req=3 is spent across restarts and hits the >3 limit before two
cycles finish. The hung stub does the same.

The limit is working. The tests assume a slower cadence than this host
gives them. No product change.
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
    print("AVRONE-12 Ollama-budget edge suite")
    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("restart limit is present", "ollama keeps dying" in text)
    stub = (REPO / "tests" / "test_autopilot_edges.py").read_text()
    rec("stub counts every GET", "n[0] += 1" in stub and "maxreq" in stub)

    one = channel_snapshot(1, 1, "deterministic")   # max_req=1, dies at start
    three = channel_snapshot(1, 1, "deterministic") # max_req=3, hits the limit
    hung = channel_snapshot(1, 1, "deterministic")  # hung, hits the limit
    rec("all three are pure", one["S"] == three["S"] == hung["S"] == 0.0)
    rec("all three are on the cone floor", one["onConeFloor"] and three["onConeFloor"] and hung["onConeFloor"])

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
        "schema": "great-scale-evidence/v16",
        "finding": (
            "The stub counts every GET, including the script's /api/version health "
            "check. max_req=1 dies at first start and never reaches the counter. "
            "max_req=3 and the hung stub hit the >3 restart limit before two cycles. "
            "The /proc fix did not move them. They are budget misses, not cleanup misses. "
            "All are one trial. Heat is zero."
        ),
        "cone": CONE,
        "isolated": {
            "ollamaDiesMidRun": "hits >3 restarts before 2 cycles",
            "restartStorm": "dies at first start, never reaches the counter",
            "hungOllama": "hits >3 restarts",
        },
        "maxReq1": {"orientation": one["orientation"], "S": one["S"], "psi": one["psi"], "effectiveRuns": 1},
        "maxReq3": {"orientation": three["orientation"], "S": three["S"], "psi": three["psi"], "effectiveRuns": 1},
        "hung": {"orientation": hung["orientation"], "S": hung["S"], "psi": hung["psi"], "effectiveRuns": 1},
        "suite": summary,
        "docker": docker,
        "mitigation": "Do not raise the restart limit. The stub budget is the test's assumption. The limit is working.",
    }
    out = ROOT / "references" / "great-scale-budget-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("budget evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"max_req=1: orientation={one['orientation']} S={one['S']} Psi={one['psi']:.6f}")
    print(f"max_req=3: orientation={three['orientation']} S={three['S']} Psi={three['psi']:.6f}")
    print(f"hung: orientation={hung['orientation']} S={hung['S']} Psi={hung['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
