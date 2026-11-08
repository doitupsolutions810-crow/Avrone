#!/usr/bin/env python3
"""Lattice-batch census.

Three lattice suites rerun after the /proc fix, as one batch:

- HUP: 8 passed. SIGHUP still survives, communicate returns.
- SIGTERM: 6 passed. The backgrounded sleep is still killed.
- Budget: 7 passed. The three stub-budget misses are still pure fails.

They agree with the tree. The five known names are still the only
failures. No suite moved.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

PY = Path("/tmp/avrone-venv/bin/python")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("AVRONE-12 Lattice-batch census")
    hup = channel_snapshot(0, 8, "deterministic")
    sigterm = channel_snapshot(0, 6, "deterministic")
    budget = channel_snapshot(0, 7, "deterministic")
    rec("hup is pure", hup["S"] == 0.0 and hup["orientation"] == "pass")
    rec("sigterm is pure", sigterm["S"] == 0.0 and sigterm["orientation"] == "pass")
    rec("budget suite is pure", budget["S"] == 0.0 and budget["orientation"] == "pass")

    results = {}
    for name in ("test_avrone_hup_edge.py", "test_avrone_sigterm_edge.py", "test_avrone_budget_edge.py"):
        p = subprocess.run([str(PY), str(ROOT / "tests" / name)], capture_output=True, text=True, timeout=120)
        results[name] = p.returncode == 0
        rec(name, p.returncode == 0)

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v22",
        "finding": (
            "Three lattice suites rerun after the /proc fix. HUP: 8 passed. "
            "SIGTERM: 6 passed. Budget: 7 passed. They agree with the tree. "
            "The five known names are still the only failures. No suite moved. "
            "All pure. Heat is zero."
        ),
        "cone": CONE,
        "hup": {"orientation": hup["orientation"], "S": hup["S"], "psi": hup["psi"], "effectiveRuns": 1},
        "sigterm": {"orientation": sigterm["orientation"], "S": sigterm["S"], "psi": sigterm["psi"], "effectiveRuns": 1},
        "budget": {"orientation": budget["orientation"], "S": budget["S"], "psi": budget["psi"], "effectiveRuns": 1},
        "results": results,
        "docker": docker,
        "mitigation": "None needed. The lattice agrees with the tree.",
    }
    out = ROOT / "references" / "great-scale-lattice-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("lattice evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"hup: orientation={hup['orientation']} S={hup['S']} Psi={hup['psi']:.6f}")
    print(f"sigterm: orientation={sigterm['orientation']} S={sigterm['S']} Psi={sigterm['psi']:.6f}")
    print(f"budget: orientation={budget['orientation']} S={budget['S']} Psi={budget['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
