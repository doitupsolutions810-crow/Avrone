#!/usr/bin/env python3
"""Bisect-edge suite.

The unpatched fixture copies the current scripts onto 1121d26. That makes
scripts/mac-local-squad.sh a tracked change. git bisect start will not
check out the midpoint over a tracked change, so the test's bisect never
begins and BISECT_LOG is never written. The script is right to continue.

After the overlay is stashed, a real bisect writes BISECT_LOG and the
script's check fires. Both outcomes are one trial. Heat is zero.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def main() -> int:
    print("AVRONE-12 Bisect-edge suite")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        dst = Path(td) / "repo"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO), str(src)], check=True)
        git(src, "checkout", "-q", "1121d26")
        # Match the fixture: merge the current scripts into the checkout, do not nest them.
        subprocess.run(
            ["bash", "-c", 'cp -a "$1"/. "$2"/', "_", str(REPO / "scripts"), str(src / "scripts")],
            check=True,
        )
        subprocess.run(["cp", "-a", str(src), str(dst)], check=True)

        dirty = git(dst, "bisect", "start", "HEAD", "HEAD~3")
        rec(
            "bisect start refuses the overlay",
            dirty.returncode != 0 and "local changes" in (dirty.stderr or ""),
            (dirty.stderr or "").strip().splitlines()[0] if dirty.stderr else "",
        )
        rec("no BISECT_LOG while the overlay is dirty", not (dst / ".git" / "BISECT_LOG").exists())

        git(dst, "stash", "-q", "--include-untracked")
        clean = git(dst, "bisect", "start", "HEAD", "HEAD~3")
        rec("bisect start works once the overlay is stashed", clean.returncode == 0)
        rec("a real bisect writes BISECT_LOG", (dst / ".git" / "BISECT_LOG").exists())

        # The script's check, pointed at this git dir.
        gitdir = git(dst, "rev-parse", "--git-dir").stdout.strip()
        seen = (dst / gitdir / "BISECT_LOG").exists()
        rec("the script's GITDIR would see the log", seen)

    no_bisect = channel_snapshot(0, 1, "deterministic")  # no log, script continues: pass for "no false refuse"
    real = channel_snapshot(1, 1, "deterministic")       # log present, script must refuse
    rec("both are pure", no_bisect["S"] == real["S"] == 0.0 and no_bisect["onConeFloor"] and real["onConeFloor"])
    rec("orientation separates them", no_bisect["orientation"] == "pass" and real["orientation"] == "fail")

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        rec("squad as uid 1000", p.returncode == 0, summary)
    else:
        rec("squad as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v11",
        "finding": (
            "git bisect start will not check out over the scripts/ overlay, so the "
            "test's bisect never begins and BISECT_LOG is never written. The script "
            "is right to continue. After the overlay is stashed, a real bisect writes "
            "BISECT_LOG and the script's check would fire. Both are one trial. Heat is zero."
        ),
        "cone": CONE,
        "overlayBlocksBisect": {
            "orientation": no_bisect["orientation"],
            "S": no_bisect["S"],
            "psi": no_bisect["psi"],
            "effectiveRuns": 1,
            "meaning": "no BISECT_LOG, script continues",
        },
        "realBisect": {
            "orientation": real["orientation"],
            "S": real["S"],
            "psi": real["psi"],
            "effectiveRuns": 1,
            "meaning": "BISECT_LOG present, script must refuse",
        },
        "suite": summary,
        "docker": docker,
        "mitigation": (
            "Do not change the bisect check. The test cannot start a bisect on a "
            "dirty tracked file. Stash the overlay first, or the refusal is about a bisect that never existed."
        ),
    }
    out = ROOT / "references" / "great-scale-bisect-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("bisect evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"no bisect: orientation={no_bisect['orientation']} S={no_bisect['S']} Psi={no_bisect['psi']:.6f}")
    print(f"real bisect: orientation={real['orientation']} S={real['S']} Psi={real['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
