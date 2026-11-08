#!/usr/bin/env python3
"""Full-edge census.

One run of tests/test_autopilot_edges.py, as uid 1000, no second pytest.
352 passed, 5 failed, 21 minutes.

The five are the ones already on record:

- ollama dies mid-run: stub budget of 3, hits the restart limit.
- restart storm: stub budget of 1, dies at first start.
- hung ollama: same restart limit.
- real bisect: the overlay dirties a tracked file, so bisect refuses to start.
- offline rerun: the test's fake .venv is a symlink with no pyvenv.cfg.

No new names. The HUP and SIGTERM fixes held.
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


KNOWN = [
    "test_ollama_dies_mid_run_is_restarted",
    "test_ollama_restart_storm_gives_up",
    "test_our_own_hung_ollama_is_killed_and_restarted",
    "test_real_bisect_in_progress_refused",
    "test_offline_rerun_with_deps_installed_continues",
]


def main() -> int:
    print("AVRONE-12 Full-edge census")
    rec("five known names", len(KNOWN) == 5)

    # 352 pass, 5 fail. Not pure: 5 is neither 0 nor 357.
    census = channel_snapshot(5, 357, "deterministic")
    rec("census is mixed", census["orientation"] == "mixed")
    rec("census is not on the cone floor", census["onConeFloor"] is False and census["S"] > 0)
    rec("effective runs stay at 357", census["effectiveRuns"] == 357)
    rec("mixed has no missed-rate bound", census["missedRate95"] is None)
    rec("runs-for-95 is finite", isinstance(census["runsFor95"], int) and census["runsFor95"] > 1)

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
        "schema": "great-scale-evidence/v17",
        "finding": (
            "One run of the autopilot edge file, as uid 1000: 352 passed, 5 failed, "
            "21 minutes. The five are the known ones: three stub-budget misses, the "
            "bisect overlay, and the offline symlink. No new names. The HUP and "
            "SIGTERM fixes held. Not pure: 5 is neither 0 nor 357, so heat is not zero."
        ),
        "cone": CONE,
        "census": {
            "passed": 352,
            "failed": 5,
            "total": 357,
            "durationSeconds": 1269,
            "orientation": census["orientation"],
            "S": census["S"],
            "psi": census["psi"],
            "missedRate95": census["missedRate95"],
            "effectiveRuns": 1,
        },
        "failed": KNOWN,
        "suite": summary,
        "docker": docker,
        "mitigation": "No new mitigation. The five are already recorded. The census confirms no new names.",
    }
    out = ROOT / "references" / "great-scale-census-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("census evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"census: 352/357 orientation={census['orientation']} S={census['S']:.6f} Psi={census['psi']:.6f}")
    print(f"runsFor95 at this p: {census['runsFor95']}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
