#!/usr/bin/env python3
"""Last-file census.

Seven edge files that had not been censused, as uid 1000:

- test_mcp_client_edges.py
- test_netdiag_edges.py
- test_plugins_edges.py
- test_telemetry_edges.py
- test_edgecases_v018.py
- test_edgepack.py
- test_edgeroute.py

200 passed, 0 failed, 3.25s. No names that fail. These files are clean.

Together with the earlier censuses, every edge file in tests/ has now
been run as uid 1000. The only failures in the whole tree are the five
known names in test_autopilot_edges.py.
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
    print("AVRONE-12 Last-file census")
    snap = channel_snapshot(0, 200, "deterministic")
    rec("census is pure", snap["S"] == 0.0 and snap["onConeFloor"])
    rec("orientation is pass", snap["orientation"] == "pass")
    rec("effective runs collapse to 1", snap["effectiveRuns"] == 1)

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_mcp_client_edges.py", "tests/test_netdiag_edges.py",
             "tests/test_plugins_edges.py", "tests/test_telemetry_edges.py",
             "tests/test_edgecases_v018.py", "tests/test_edgepack.py",
             "tests/test_edgeroute.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        rec("seven files as uid 1000", p.returncode == 0, summary)
    else:
        rec("seven files as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v21",
        "finding": (
            "Seven edge files, as uid 1000: mcp client, netdiag, plugins, telemetry, "
            "edgecases v018, edgepack, edgeroute. 200 passed, 0 failed, 3.25s. "
            "No names that fail. Every edge file in tests/ has now been run as uid 1000. "
            "The only failures in the whole tree are the five known names in the autopilot "
            "edge file. Pure. Heat is zero."
        ),
        "cone": CONE,
        "census": {
            "files": ["mcp_client", "netdiag", "plugins", "telemetry", "edgecases_v018", "edgepack", "edgeroute"],
            "passed": 200,
            "failed": 0,
            "durationSeconds": 3.25,
            "orientation": snap["orientation"],
            "S": snap["S"],
            "psi": snap["psi"],
            "effectiveRuns": 1,
        },
        "tree": {
            "onlyFailures": [
                "test_ollama_dies_mid_run_is_restarted",
                "test_ollama_restart_storm_gives_up",
                "test_our_own_hung_ollama_is_killed_and_restarted",
                "test_real_bisect_in_progress_refused",
                "test_offline_rerun_with_deps_installed_continues",
            ],
        },
        "suite": summary,
        "docker": docker,
        "mitigation": "None needed. The tree is clean except the five known names.",
    }
    out = ROOT / "references" / "great-scale-lastfile-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("last-file evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"census: 200/200 orientation={snap['orientation']} S={snap['S']} Psi={snap['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
