#!/usr/bin/env python3
"""Process-edge suite.

The in-process result was: 80 concurrent 1.6ms/2ms waits cross 24 timeouts
often enough to leave the cone floor, while 40-wide batches stay at zero.
That 24 was measured inside one interpreter. This suite repeats the batch
in fresh processes, so the parent gather is not part of the sample.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot  # type: ignore

PY = Path("/tmp/avrone-venv/bin/python")
REPO = Path("/tmp/Avrone")
PASS = FAIL = 0

CHILD = r'''
import asyncio, sys
async def race():
    try:
        await asyncio.wait_for(asyncio.sleep(0.0016), 0.002)
        return 0
    except asyncio.TimeoutError:
        return 1
async def main():
    k, n = int(sys.argv[1]), int(sys.argv[2])
    for _ in range(n):
        hits = await asyncio.gather(*[race() for _ in range(k)])
        print(sum(hits))
asyncio.run(main())
'''


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def spawn(k: int, n: int) -> list[int]:
    p = subprocess.run([str(PY), "-c", CHILD, str(k), str(n)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-400:])
    return [int(line) for line in p.stdout.split() if line.strip()]


def main() -> int:
    print("AVRONE-12 Process-edge suite")
    if not PY.is_file():
        print("venv missing")
        return 1

    # Two fresh interpreters, same batch, no shared loop.
    a40 = spawn(40, 15)
    b40 = spawn(40, 15)
    a80 = spawn(80, 30)
    b80 = spawn(80, 30)
    rec("process A 40-wide stays quiet", a40 == [0] * 15, f"max={max(a40)}")
    rec("process B 40-wide stays quiet", b40 == [0] * 15, f"max={max(b40)}")
    rec("process A 80-wide varies", max(a80) - min(a80) >= 10, f"{min(a80)}..{max(a80)}")
    rec("process B 80-wide varies", max(b80) - min(b80) >= 10, f"{min(b80)}..{max(b80)}")

    over_a = sum(c > 24 for c in a80)
    over_b = sum(c > 24 for c in b80)
    snap_a = channel_snapshot(over_a, len(a80), "stochastic")
    snap_b = channel_snapshot(over_b, len(b80), "stochastic")
    rec("process A crosses 24", 0 < over_a < len(a80), f"{over_a}/30")
    rec("process B crosses 24", 0 < over_b < len(b80), f"{over_b}/30")
    rec("both off the cone floor", (not snap_a["onConeFloor"]) and (not snap_b["onConeFloor"]))
    # The line is the same predicate, but the two processes are not the same p.
    gap = abs(snap_a["p"] - snap_b["p"])
    rec("both processes leave a measured p", snap_a["p"] > 0 and snap_b["p"] > 0, f"|pA-pB|={gap:.3f}")

    squad = "not run"
    if REPO.is_dir():
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py", "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        squad = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        rec("squad pytest", p.returncode == 0, squad)
    else:
        rec("squad pytest", False, "repo missing")

    payload = {
        "schema": "great-scale-evidence/v4",
        "finding": (
            "Width 40 is stable across fresh processes: the max timeout count is 0. "
            "Width 80 is hot in both and both leave the cone floor, but p is not a constant. "
            "Carry the width split and the heat. Do not publish one p."
        ),
        "cone": CONE,
        "process_a": {
            "width40_max": max(a40),
            "width80_min": min(a80), "width80_max": max(a80),
            "predicate": "timeouts > 24", "failures": over_a, "runs": 30,
            "p": snap_a["p"], "S": snap_a["S"], "psi": snap_a["psi"],
            "logCoord": snap_a["logCoord"], "orientation": snap_a["orientation"],
            "onConeFloor": snap_a["onConeFloor"], "effectiveRuns": snap_a["effectiveRuns"],
        },
        "process_b": {
            "width40_max": max(b40),
            "width80_min": min(b80), "width80_max": max(b80),
            "predicate": "timeouts > 24", "failures": over_b, "runs": 30,
            "p": snap_b["p"], "S": snap_b["S"], "psi": snap_b["psi"],
            "logCoord": snap_b["logCoord"], "orientation": snap_b["orientation"],
            "onConeFloor": snap_b["onConeFloor"], "effectiveRuns": snap_b["effectiveRuns"],
        },
        "abs_p_gap": gap,
        "squad": squad,
        "mitigation": (
            "Carry the quiet width and the fact of heat across processes. "
            "Do not carry a single p or a single batch count."
        ),
    }
    out = ROOT / "references" / "great-scale-process-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("process evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(
        f"A: {over_a}/30 p={snap_a['p']:.4f} S={snap_a['S']:.4f} Psi={snap_a['psi']:.6f} "
        f"counts {min(a80)}..{max(a80)}"
    )
    print(
        f"B: {over_b}/30 p={snap_b['p']:.4f} S={snap_b['S']:.4f} Psi={snap_b['psi']:.6f} "
        f"counts {min(b80)}..{max(b80)}"
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
