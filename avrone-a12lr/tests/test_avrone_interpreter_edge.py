#!/usr/bin/env python3
"""Interpreter-edge suite.

The evolve gate used to shell out to bare `python`. On this machine that
interpreter cannot import pytest, so every proposal was rejected. The running
interpreter can. Both outcomes sit on the cone floor. Orientation is the
difference. The mitigation is sys.executable, one token after shlex quoting.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, "/tmp/Avrone")
from evidence_scale import CONE, channel_snapshot  # type: ignore

PY = Path("/tmp/avrone-venv/bin/python")
REPO = Path("/tmp/Avrone")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def can_import_pytest(exe: str) -> bool:
    p = subprocess.run([exe, "-c", "import pytest"], capture_output=True, text=True)
    return p.returncode == 0


def main() -> int:
    print("AVRONE-12 Interpreter-edge suite")
    path_py = subprocess.run(["bash", "-lc", "command -v python"], capture_output=True, text=True).stdout.strip()
    rec("PATH python is not the running interpreter", path_py != sys.executable, path_py)
    path_ok = can_import_pytest(path_py) if path_py else False
    here_ok = can_import_pytest(sys.executable)
    rec("PATH python cannot import pytest", not path_ok, path_py)
    rec("running interpreter can import pytest", here_ok, sys.executable)

    path_snap = channel_snapshot(0 if path_ok else 1, 1, "deterministic")
    here_snap = channel_snapshot(0 if here_ok else 1, 1, "deterministic")
    rec("both sits on the cone floor", path_snap["onConeFloor"] and here_snap["onConeFloor"])
    rec("heat is zero either way", path_snap["S"] == here_snap["S"] == 0.0)
    rec(
        "orientation separates them",
        path_snap["orientation"] == "fail" and here_snap["orientation"] == "pass",
    )

    import avrone.evolve as evolve

    cmd = evolve.default_test_cmd()
    parts = shlex.split(cmd)
    rec("default starts with this interpreter", parts[0] == sys.executable, parts[0])
    rec("default is one executable token", len(parts) >= 4 and parts[1:4] == ["-m", "pytest", "-q"])
    spaced = "/tmp/a dir/python"
    quoted = shlex.quote(spaced)
    rec("a spaced path stays one token", shlex.split(f"{quoted} -m pytest")[0] == spaced)

    # Operator override still wins. That is the explicit channel, not PATH.
    old = os.environ.get("AVRONE_EVOLVE_TEST_CMD")
    os.environ["AVRONE_EVOLVE_TEST_CMD"] = "false"
    try:
        rec("env override beats the default", evolve._resolve_test_cmd(None) == "false")
        rec("explicit argument beats the env", evolve._resolve_test_cmd("true") == "true")
    finally:
        if old is None:
            os.environ.pop("AVRONE_EVOLVE_TEST_CMD", None)
        else:
            os.environ["AVRONE_EVOLVE_TEST_CMD"] = old

    evolve_summary = "not run"
    squad_summary = "not run"
    if PY.is_file() and REPO.is_dir():
        env = os.environ.copy()
        env.pop("AVRONE_EVOLVE_TEST_CMD", None)
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_evolve.py", "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=180, env=env,
        )
        evolve_summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else (p.stderr or "")[-300:]
        rec("evolve default and squad pytest", p.returncode == 0, evolve_summary)
        squad_summary = evolve_summary
    else:
        rec("evolve default and squad pytest", False, "missing")

    docker = subprocess.run(
        ["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"],
        capture_output=True,
    ).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v5",
        "finding": (
            "PATH python and the running interpreter are two pure predicates. "
            "Both have S = 0. One fails, one passes. The gate must call the one that passes."
        ),
        "cone": CONE,
        "path_python": {
            "executable": path_py,
            "canImportPytest": path_ok,
            "S": path_snap["S"],
            "psi": path_snap["psi"],
            "orientation": path_snap["orientation"],
            "effectiveRuns": path_snap["effectiveRuns"],
            "onConeFloor": path_snap["onConeFloor"],
        },
        "running_interpreter": {
            "executable": sys.executable,
            "canImportPytest": here_ok,
            "S": here_snap["S"],
            "psi": here_snap["psi"],
            "orientation": here_snap["orientation"],
            "effectiveRuns": here_snap["effectiveRuns"],
            "onConeFloor": here_snap["onConeFloor"],
        },
        "defaultTestCmd": cmd,
        "evolveAndSquad": evolve_summary,
        "squad": squad_summary,
        "docker": docker,
        "mitigation": (
            "default_test_cmd quotes sys.executable. "
            "AVRONE_EVOLVE_TEST_CMD and an explicit argument still override it."
        ),
    }
    out = ROOT / "references" / "great-scale-interpreter-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("interpreter evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(
        f"PATH {path_py}: orientation={path_snap['orientation']} S={path_snap['S']} "
        f"Psi={path_snap['psi']:.6f}"
    )
    print(
        f"running {sys.executable}: orientation={here_snap['orientation']} S={here_snap['S']} "
        f"Psi={here_snap['psi']:.6f}"
    )
    print(f"default: {cmd}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
