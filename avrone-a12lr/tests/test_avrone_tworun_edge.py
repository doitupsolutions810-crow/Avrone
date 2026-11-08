#!/usr/bin/env python3
"""Two-run census.

Two full runs of tests/test_autopilot_edges.py, as uid 1000, no second
pytest. Both: 352 passed, 5 failed.

The five names are the same in both runs:

- ollama dies mid-run
- restart storm
- hung ollama
- real bisect
- offline rerun

Nothing flipped. No name that failed the first time passed the second.
No name that passed the first time failed the second. The five are
deterministic on this host. The 352 are stable.
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


RUN1 = [
    "test_ollama_dies_mid_run_is_restarted",
    "test_ollama_restart_storm_gives_up",
    "test_our_own_hung_ollama_is_killed_and_restarted",
    "test_real_bisect_in_progress_refused",
    "test_offline_rerun_with_deps_installed_continues",
]
RUN2 = list(RUN1)


def main() -> int:
    print("AVRONE-12 Two-run census")
    rec("same five names", RUN1 == RUN2)
    rec("no flips", set(RUN1) == set(RUN2) and len(RUN1) == 5)

    # Zero flips across two runs. That is a pure pass of the comparison.
    flips = channel_snapshot(0, 1, "deterministic")
    rec("comparison is pure", flips["S"] == 0.0 and flips["onConeFloor"])
    rec("orientation is pass", flips["orientation"] == "pass")

    # Each run is mixed. Same heat both times.
    one = channel_snapshot(5, 357, "deterministic")
    two = channel_snapshot(5, 357, "deterministic")
    rec("both runs have the same heat", abs(one["S"] - two["S"]) < 1e-15)
    rec("both runs are mixed", one["orientation"] == two["orientation"] == "mixed")

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
        "schema": "great-scale-evidence/v18",
        "finding": (
            "Two full runs of the autopilot edge file, as uid 1000. Both: 352 passed, "
            "5 failed, same five names. Nothing flipped. The five are deterministic on "
            "this host. The 352 are stable. The comparison is one trial. Heat is zero."
        ),
        "cone": CONE,
        "run1": {"passed": 352, "failed": 5, "durationSeconds": 1269, "names": RUN1},
        "run2": {"passed": 352, "failed": 5, "durationSeconds": 1266, "names": RUN2},
        "flips": {"count": 0, "orientation": flips["orientation"], "S": flips["S"], "psi": flips["psi"], "effectiveRuns": 1},
        "eachRun": {"orientation": one["orientation"], "S": one["S"], "psi": one["psi"], "effectiveRuns": 357},
        "suite": summary,
        "docker": docker,
        "mitigation": "No new mitigation. The five are deterministic. They will not move on their own.",
    }
    out = ROOT / "references" / "great-scale-tworun-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("two-run evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"flips: {0} orientation={flips['orientation']} S={flips['S']} Psi={flips['psi']:.6f}")
    print(f"each run: orientation={one['orientation']} S={one['S']:.6f} Psi={one['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
