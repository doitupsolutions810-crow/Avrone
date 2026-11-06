#!/usr/bin/env python3
"""Scale-edge suite: new gate cases, then map the count onto the Great Scale."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, evidence_snapshot, heat, load_repo_greatscale  # type: ignore

BIN = ROOT / "bin"
MASTER = BIN / "activate_avrone.sh"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"

PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(cmd, env=None, clear=False, timeout=8.0):
    e = os.environ.copy()
    if clear:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def main() -> int:
    print("AVRONE-12 Scale-edge suite")
    checks = 0
    bad = 0

    def tally(ok: bool) -> None:
        nonlocal checks, bad
        checks += 1
        bad += int(not ok)

    # Gate: only exact ASCII true. These strings were not in the rift set.
    refuse_vals = [
        "true\r",
        "\ufefftrue",
        "true%00",
        "true' OR '1'='1",
        "true && true",
        "eurt",
        "true\ttrue",
        "\u200etrue",
    ]
    for val in refuse_vals:
        code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": val})
        ok = code not in (0, -1)
        rec(f"refuse {val!r}", ok, f"code={code}")
        tally(ok)

    code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
    ok = code == 0 and "DEPLOYMENT_COMPLETE" in out
    rec("exact true accepts", ok)
    tally(ok)

    code, out, err = run(["bash", str(MASTER), "status"], clear=True)
    ok = code != 0 and "DEPLOYMENT_COMPLETE" not in out
    rec("unset refuses and hides COMPLETE", ok)
    tally(ok)

    code, out, err = run([sys.executable, str(BRIDGE)], env={"ALLOW_AGENT_EXECUTION": "true"})
    data = json.loads(out)
    ok = data["status"] == "DEPLOYMENT_COMPLETE" and data["agents"]["count"] == 6
    rec("bridge six agents", ok)
    tally(ok)

    code, out, err = run(
        ["bash", str(BIN / "activate_control12_lattice_op.sh"), "--dry-run", "--dry-run"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    ok = code == 0 and "control12_lattice_op" in out
    rec("double dry-run still gated object", ok)
    tally(ok)

    # Laws, executed, not narrated.
    rec("heat symmetric", abs(heat(0.2) - heat(0.8)) < 1e-15 and abs(heat(0.2) - 0.16) < 1e-15)
    rec("heat peaks at one half", heat(0.5) == 0.25)
    rec("cone is phi^-2", abs(CONE - ((1 + 5**0.5) / 2 - 1) ** 2) < 1e-15)
    tally(abs(heat(0.2) - heat(0.8)) < 1e-15)
    tally(heat(0.5) == 0.25)
    tally(abs(CONE - 0.3819660112501051) < 1e-12)

    clean = evidence_snapshot(0, checks)
    flipped = evidence_snapshot(checks, checks)
    rec("clean run on cone floor", clean["onConeFloor"] and clean["orientation"] == "pass")
    rec("all-fail also on cone floor", flipped["onConeFloor"] and flipped["S"] == 0.0)
    rec("orientation separates them", clean["orientation"] != flipped["orientation"])
    rec("zero failures is not a proof of p=0", clean["missedRate95"] is not None and clean["missedRate95"] > 0)
    # A 1-in-200 defect needs 598 runs; this suite is shorter, so the bound must say so.
    rec("suite shorter than 598", checks < 598)

    gs = load_repo_greatscale()
    if gs is None:
        rec("parity with repo greatscale", False, "module missing")
    else:
        peer = gs.evidence_snapshot(0, checks)
        rec(
            "parity with repo greatscale",
            peer["orientation"] == "pass" and abs(peer["psi"] - clean["psi"]) < 1e-12,
            f"psi={clean['psi']:.6f}",
        )

    out_path = ROOT / "references" / "great-scale-evidence.json"
    payload = {
        "schema": "great-scale-evidence/v1",
        "source": "avrone-a12lr scale-edge suite",
        "peer": "avrone/greatscale.py evidence_snapshot",
        "observed": clean,
        "orientation_control_all_fail": {
            "orientation": flipped["orientation"],
            "onConeFloor": flipped["onConeFloor"],
            "S": flipped["S"],
        },
        "mitigation": (
            "Do not read the cone floor as correctness. Read orientation. "
            "Do not read zero failures as p=0. Read missedRate95."
        ),
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    rec("evidence file written", out_path.is_file())

    print(f"Results: {PASS} passed · {FAIL} failed · checks_in_snapshot={checks} · missedRate95={clean['missedRate95']:.6g}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
