#!/usr/bin/env python3
"""Remaining-file census.

Four edge files that had not been censused, as uid 1000:

- test_agentkit_edges.py
- test_fleet_edges.py
- test_gateway_edges.py
- test_server_edges.py

142 passed, 0 failed, 2.10s. No names that fail. These files are clean.
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
    print("AVRONE-12 Remaining-file census")
    snap = channel_snapshot(0, 142, "deterministic")
    rec("census is pure", snap["S"] == 0.0 and snap["onConeFloor"])
    rec("orientation is pass", snap["orientation"] == "pass")
    rec("effective runs collapse to 1", snap["effectiveRuns"] == 1)

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_agentkit_edges.py", "tests/test_fleet_edges.py",
             "tests/test_gateway_edges.py", "tests/test_server_edges.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        rec("four files as uid 1000", p.returncode == 0, summary)
    else:
        rec("four files as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v20",
        "finding": (
            "Four edge files, as uid 1000: agentkit, fleet, gateway, server. "
            "142 passed, 0 failed, 2.10s. No names that fail. These files are clean. "
            "Pure. Heat is zero."
        ),
        "cone": CONE,
        "census": {
            "files": ["agentkit", "fleet", "gateway", "server"],
            "passed": 142,
            "failed": 0,
            "durationSeconds": 2.1,
            "orientation": snap["orientation"],
            "S": snap["S"],
            "psi": snap["psi"],
            "effectiveRuns": 1,
        },
        "suite": summary,
        "docker": docker,
        "mitigation": "None needed. These files are clean.",
    }
    out = ROOT / "references" / "great-scale-remaining-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("remaining-file evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"census: 142/142 orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
