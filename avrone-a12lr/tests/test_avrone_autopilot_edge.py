#!/usr/bin/env python3
"""Autopilot-edge suite.

The evolve default now uses sys.executable. The autopilot then wrote
AVRONE_EVOLVE_TEST_CMD="python -m pytest ..." into env.sh, and later
processes source that file without activating the venv. Bare python is
PATH again. The export must carry the absolute venv interpreter, including
when the path contains a space.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

PY = Path("/tmp/avrone-venv/bin/python")
REPO = Path("/tmp/Avrone")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("AVRONE-12 Autopilot-edge suite")
    for rel in ("scripts/mac-avrone-autopilot.sh", "scripts/mac-local-squad.sh"):
        p = subprocess.run(["bash", "-n", str(REPO / rel)], capture_output=True, text=True)
        rec(f"bash -n {rel}", p.returncode == 0, (p.stderr or "").strip())

    auto = (REPO / "scripts/mac-avrone-autopilot.sh").read_text()
    squad = (REPO / "scripts/mac-local-squad.sh").read_text()
    rec("autopilot export is not bare python", 'AVRONE_EVOLVE_TEST_CMD="python -m pytest' not in auto)
    rec("autopilot export quotes the venv interpreter", 'AVRONE_EVOLVE_TEST_CMD="$(q "$VENV_PY") -m pytest' in auto)
    rec("squad gate uses VENV_PY", '"$VENV_PY" -m pytest' in squad)
    rec("squad lane uses VENV_PY", '"$VENV_PY" -m avrone.cli' in squad)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo dir"
        state = Path(td) / "state"
        (root / ".venv" / "bin").mkdir(parents=True)
        stub = root / ".venv" / "bin" / "python"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        state.mkdir()
        script = r'''
q(){ printf '%q' "$1"; }
VENV_PY="$1"
cat > "$2/env.sh" << EOF
export AVRONE_EVOLVE_TEST_CMD="$(q "$VENV_PY") -m pytest -q -x -p no:cacheprovider -m 'not integration' tests"
EOF
'''
        subprocess.run(["bash", "-c", script, "q", str(stub), str(state)], check=True)
        probe = subprocess.run(
            ["bash", "-c",
             'set -eu; source "$1"; python3 -c "import os,shlex; p=shlex.split(os.environ[\'AVRONE_EVOLVE_TEST_CMD\']); print(p[0]); print(\' \'.join(p[1:5]))"',
             "_", str(state / "env.sh")],
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "AVRONE_PROBE": td},
        )
        lines = (probe.stdout or "").splitlines()
        rec("sourced command parses", probe.returncode == 0 and len(lines) == 2, probe.stderr[-200:])
        first = lines[0] if lines else ""
        rest = lines[1] if len(lines) > 1 else ""
        rec("first token is the venv python, spaces included", first == str(stub), first)
        rec("first token is not bare python", first != "python")
        rec("pytest args survive quoting", rest == "-m pytest -q -x", rest)

    path_py = subprocess.run(["bash", "-lc", "command -v python"], capture_output=True, text=True).stdout.strip()
    path_rc = subprocess.run([path_py, "-c", "import pytest"], capture_output=True).returncode if path_py else 1
    venv_rc = subprocess.run([str(PY), "-c", "import pytest"], capture_output=True).returncode
    path_snap = channel_snapshot(0 if path_rc == 0 else 1, 1, "deterministic")
    venv_snap = channel_snapshot(0 if venv_rc == 0 else 1, 1, "deterministic")
    rec("PATH still cannot import pytest", path_snap["orientation"] == "fail" and path_snap["S"] == 0)
    rec("venv can, and both are on the floor", venv_snap["orientation"] == "pass" and venv_snap["onConeFloor"] and path_snap["onConeFloor"])

    summary = "not run"
    if PY.is_file():
        env = os.environ.copy()
        env.pop("AVRONE_EVOLVE_TEST_CMD", None)
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py", "tests/test_evolve.py",
             "tests/test_autopilot_edges.py::test_happy_path_with_spaces_in_drive",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=240, env=env,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else (p.stderr or "")[-400:]
        if p.returncode != 0:
            tail = (p.stdout or "")[-1500:]
            print(tail)
        rec("squad, evolve, and autopilot happy path", p.returncode == 0, summary)
    else:
        rec("squad, evolve, and autopilot happy path", False, "venv missing")

    docker = subprocess.run(
        ["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"],
        capture_output=True,
    ).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v6",
        "finding": (
            "Sourcing env.sh is a different process from the one that activated the venv. "
            "A bare python in that file is the PATH predicate (orientation fail, S = 0). "
            "The absolute venv path is the pass predicate, and it survives a space in the path."
        ),
        "cone": CONE,
        "path_python": {"executable": path_py, "orientation": path_snap["orientation"], "S": path_snap["S"], "psi": path_snap["psi"], "effectiveRuns": 1},
        "venv_python": {"executable": str(PY), "orientation": venv_snap["orientation"], "S": venv_snap["S"], "psi": venv_snap["psi"], "effectiveRuns": 1},
        "sourcedFirstTokenHasSpace": " " in first,
        "suite": summary,
        "docker": docker,
        "mitigation": "AVRONE_EVOLVE_TEST_CMD in env.sh is the quoted absolute venv interpreter plus the pytest args.",
    }
    out = ROOT / "references" / "great-scale-autopilot-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("autopilot evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"PATH {path_py}: orientation={path_snap['orientation']} S={path_snap['S']} Psi={path_snap['psi']:.6f}")
    print(f"venv {PY}: orientation={venv_snap['orientation']} S={venv_snap['S']} Psi={venv_snap['psi']:.6f}")
    print(f"sourced first token: {first}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
