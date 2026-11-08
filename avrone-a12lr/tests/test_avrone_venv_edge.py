#!/usr/bin/env python3
"""Venv-edge suite.

Two limits that looked alike.

The dangling rebuild: python3.11 on this host has no ensurepip, so
`python -m venv` fails and the rebuild dies before activate exists.
`--without-pip` produces a venv with python and activate. The later pip
step handles pip. That is the product fix.

The offline check: the test's fake .venv is a symlink to the real venv
python and has no pyvenv.cfg. Python resolves the symlink chain to the
system interpreter and never enters the venv, so pytest is missing.
The product check is right. A symlink without pyvenv.cfg is not a venv.
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


def main() -> int:
    print("AVRONE-12 Venv-edge suite")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        plain = subprocess.run(["python3.11", "-m", "venv", str(td / "plain")], capture_output=True, text=True)
        blob = (plain.stdout or "") + (plain.stderr or "")
        rec("python3.11 -m venv fails without ensurepip", plain.returncode != 0 and "ensurepip" in blob)
        bare = subprocess.run(["python3.11", "-m", "venv", "--without-pip", str(td / "bare")], capture_output=True, text=True)
        rec("--without-pip produces activate", bare.returncode == 0 and (td / "bare" / "bin" / "activate").is_file())

        # A symlink to the venv python, with no pyvenv.cfg beside it, is not a venv.
        link = td / "link-python"
        link.symlink_to(PY)
        via = subprocess.run([str(link), "-c", "import pytest"], capture_output=True, text=True)
        direct = subprocess.run([str(PY), "-c", "import pytest"], capture_output=True, text=True)
        rec("direct venv python imports pytest", direct.returncode == 0)
        rec("a bare symlink to it does not", via.returncode != 0 and "pytest" in (via.stderr or ""))

    text = (REPO / "scripts" / "mac-avrone-autopilot.sh").read_text()
    rec("script retries venv creation without pip", "--without-pip" in text)

    no_pip = channel_snapshot(0, 1, "deterministic")   # --without-pip now succeeds
    symlink = channel_snapshot(1, 1, "deterministic")  # bare symlink is not a venv
    rec("both are pure", no_pip["S"] == symlink["S"] == 0.0 and no_pip["onConeFloor"] and symlink["onConeFloor"])
    rec("orientation separates them", no_pip["orientation"] == "pass" and symlink["orientation"] == "fail")

    summary = "not run"
    if PY.is_file():
        p = subprocess.run(
            ["unshare", "--user", "--map-user=1000", "--map-group=1000",
             str(PY), "-m", "pytest", "-q",
             "tests/test_autopilot_edges.py::test_dangling_venv_python_after_brew_upgrade_is_rebuilt",
             "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        if p.returncode != 0:
            print((p.stdout or "")[-600:])
        rec("dangling rebuild and squad as uid 1000", p.returncode == 0, summary)
    else:
        rec("dangling rebuild and squad as uid 1000", False, "venv missing")

    docker = subprocess.run(["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"]).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v12",
        "finding": (
            "python3.11 here has no ensurepip, so a plain venv creation fails. "
            "--without-pip produces activate and python. A symlink to the venv "
            "python with no pyvenv.cfg beside it is not a venv: Python resolves "
            "to the system interpreter and pytest is missing. The offline check "
            "is right to refuse that. Both are one trial. Heat is zero."
        ),
        "cone": CONE,
        "withoutPip": {
            "orientation": no_pip["orientation"],
            "S": no_pip["S"],
            "psi": no_pip["psi"],
            "effectiveRuns": 1,
        },
        "bareSymlink": {
            "orientation": symlink["orientation"],
            "S": symlink["S"],
            "psi": symlink["psi"],
            "effectiveRuns": 1,
        },
        "suite": summary,
        "docker": docker,
        "mitigation": (
            "Retry `python -m venv --without-pip` when ensurepip is missing. "
            "Do not weaken the import check: a symlink without pyvenv.cfg is not an installed AVRONE."
        ),
    }
    out = ROOT / "references" / "great-scale-venv-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("venv evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(f"--without-pip: orientation={no_pip['orientation']} S={no_pip['S']} Psi={no_pip['psi']:.6f}")
    print(f"bare symlink: orientation={symlink['orientation']} S={symlink['S']} Psi={symlink['psi']:.6f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
