#!/usr/bin/env python3
"""Shear-edge suite.

Finding: repeating a pure gate does not multiply independent Bernoulli trials.
The miss bound p < 1 - exp(ln(alpha)/N) is valid only when each run can flip.
A deterministic activator that emits identical bytes is one trial. Using the
copy count as N reports a tighter bound than the sample contains.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import channel_snapshot  # type: ignore

MASTER = ROOT / "bin" / "activate_avrone.sh"
REPO = Path("/tmp/Avrone")
PY = Path("/tmp/avrone-venv/bin/python")

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
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except ValueError as exc:
        return -2, "", f"ValueError:{exc}"


def main() -> int:
    print("AVRONE-12 Shear-edge suite")
    copies = 24
    hashes = set()
    codes = []
    for _ in range(copies):
        code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        codes.append(code)
        hashes.add(hashlib.sha256(out.encode()).hexdigest())
    rec("24 status copies exit 0", codes == [0] * copies)
    rec("24 status copies are byte-identical", len(hashes) == 1, f"unique={len(hashes)}")

    det = channel_snapshot(0, copies, "deterministic")
    rec("deterministic effective N is 1", det["effectiveRuns"] == 1)
    rec(
        "naive bound is illegally tighter",
        det["naiveMissedRate95"] < det["missedRate95"],
        f"naive={det['naiveMissedRate95']:.4f} honest={det['missedRate95']:.4f}",
    )
    rec("honest single-trial bound is 0.95", abs(det["missedRate95"] - (1 - pow(0.05, 1))) < 1e-12)

    # Overlap of two pure predicates. Copies still do not create a rate sample.
    def accept(_i):
        code, out, _ = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": "true"})
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def refuse(_i):
        code, out, _ = run(["bash", str(MASTER), "status"], clear=True)
        return code not in (0, -1) and "DEPLOYMENT_COMPLETE" not in out

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(accept, range(12)))
        refused = list(pool.map(refuse, range(12)))
    rec("concurrent accept holds", accepted == [True] * 12)
    rec("concurrent refuse holds", refused == [True] * 12)
    opened = channel_snapshot(accepted.count(False), 12, "deterministic")
    closed = channel_snapshot(refused.count(False), 12, "deterministic")
    rec("two pure predicates stay at N=1 each", opened["effectiveRuns"] == closed["effectiveRuns"] == 1)
    pooled = channel_snapshot(0, 24, "stochastic")
    rec(
        "pooling the copies would fake a 24-trial bound",
        pooled["missedRate95"] < opened["missedRate95"],
        f"fake p<{pooled['missedRate95']:.4f} honest p<{opened['missedRate95']:.4f}",
    )

    # New gate strings, not used in the rift or scale suites.
    for val in ["true\v", "true\f", "true\u0301", "true\u000btrue", "\u0000true"]:
        code, out, err = run(["bash", str(MASTER), "status"], env={"ALLOW_AGENT_EXECUTION": val})
        rec(f"refuse {val!r}", code not in (0, -1), f"code={code}")

    code, out, err = run(["bash", str(MASTER), "."], env={"ALLOW_AGENT_EXECUTION": "true"})
    rec("dot component is not status", code != 0 and "components_registered" not in out, f"code={code}")

    squad = {"ran": False, "passed": None, "failed": None}
    if PY.is_file() and (REPO / "tests" / "test_robust_squad.py").is_file():
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_greatscale.py", "tests/test_robust_squad.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        tail = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        squad = {"ran": True, "exit": p.returncode, "summary": tail}
        rec("squad and greatscale pytest", p.returncode == 0, tail)
    else:
        rec("squad and greatscale pytest", False, "venv or repo missing")

    docker_up = subprocess.run(["bash", "-lc", "command -v docker && test -S /var/run/docker.sock"],
                               capture_output=True).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker_up else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v2",
        "finding": (
            "A pure function repeated N times is one Bernoulli trial. "
            "Two pure predicates do not pool into one rate. "
            "Only a check that can flip under the same input earns N greater than 1."
        ),
        "deterministic_gate": det,
        "open_gate_predicate": {"copies": 12, "effectiveRuns": opened["effectiveRuns"], "missedRate95": opened["missedRate95"]},
        "closed_gate_predicate": {"copies": 12, "effectiveRuns": closed["effectiveRuns"], "missedRate95": closed["missedRate95"]},
        "illegal_pooled_bound": {"runs": pooled["runs"], "missedRate95": pooled["missedRate95"], "channel": "stochastic-if-mislabelled"},
        "squad": squad,
        "docker": docker_up,
        "mitigation": (
            "Tag a check stochastic only when the same input can flip. "
            "Do not buy a tighter bound by repeating a script, or by pooling two pure predicates."
        ),
    }
    out = ROOT / "references" / "great-scale-shear-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("shear evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(
        f"deterministic copies={copies} effectiveRuns={det['effectiveRuns']} "
        f"naive p<{det['naiveMissedRate95']:.4f} honest p<{det['missedRate95']:.4f}"
    )
    print(
        f"open predicate copies=12 effectiveRuns={opened['effectiveRuns']} honest p<{opened['missedRate95']:.4f}; "
        f"illegal pool of 24 would claim p<{pooled['missedRate95']:.4f}"
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
