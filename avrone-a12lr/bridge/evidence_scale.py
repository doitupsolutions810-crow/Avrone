#!/usr/bin/env python3
"""Map AVRONE-12 lattice evidence onto the Great Scale residual coordinate.

Does not modify avrone/greatscale.py. When that module can be imported it is
the source of the numbers; otherwise the same laws are evaluated locally:

    S   = heat(p) = p(1-p)          Bernoulli variance of a check
    4S  = log coordinate
    Psi = CONE + S,  CONE = phi^-2

Heat is orientation-blind: heat(p) = heat(1-p). An all-pass suite and an
all-fail suite both sit on the cone floor. Correctness is the signed channel
`orientation`. Zero failures in N runs does not prove p = 0; the 95% miss
bound is p < 1 - exp(ln(0.05)/N).
"""
from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from typing import Any

PHI = (1 + math.sqrt(5)) / 2
CONE = (PHI - 1) ** 2


def heat(h: float) -> float:
    x = max(0.0, min(1.0, float(h)))
    return x * (1.0 - x)


def evidence_snapshot(failures: int, runs: int, alpha: float = 0.05) -> dict[str, Any]:
    if not (1 <= runs) or not (0 <= failures <= runs):
        raise ValueError("need runs >= 1 and 0 <= failures <= runs")
    p = failures / runs
    s = heat(p)
    if p == 0:
        runs_for_95 = None
        missed_p_95 = 1.0 - math.exp(math.log(alpha) / runs)
    elif p == 1:
        runs_for_95 = 1
        missed_p_95 = 0.0
    else:
        runs_for_95 = max(1, math.ceil(math.log(alpha) / math.log(1.0 - p) - 1e-12))
        missed_p_95 = None
    return {
        "failures": failures,
        "runs": runs,
        "p": p,
        "S": s,
        "logCoord": 4 * s,
        "psi": CONE + s,
        "cone": CONE,
        "coneOk": (CONE + s) >= CONE - 1e-12,
        "onConeFloor": s == 0.0,
        "orientation": "pass" if failures == 0 else ("fail" if failures == runs else "mixed"),
        "n": 8 - 4 * s,
        "lambda": PHI ** (-(8 - 4 * s)),
        "singleRunDetect": p,
        "detectInRuns": 1 - (1 - p) ** runs,
        "runsFor95": runs_for_95,
        "missedRate95": missed_p_95,
    }


def load_repo_greatscale():
    """Import avrone.greatscale from a sibling checkout without pulling httpx."""
    here = Path(__file__).resolve()
    for root in (here.parents[2], Path("/tmp/Avrone")):
        pkg = root / "avrone" / "greatscale.py"
        if not pkg.is_file():
            continue
        name = "avrone"
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = [str(root / "avrone")]  # type: ignore[attr-defined]
            sys.modules[name] = mod
        tools_name = "avrone.tools"
        if tools_name not in sys.modules:
            tools = types.ModuleType(tools_name)
            tools.Tool = type("Tool", (), {})  # type: ignore[attr-defined]
            sys.modules[tools_name] = tools
        sys.path.insert(0, str(root))
        import avrone.greatscale as gs  # type: ignore

        return gs
    return None


def card(failures: int, runs: int, label: str) -> str:
    snap = evidence_snapshot(failures, runs)
    n95 = snap["runsFor95"]
    miss = snap["missedRate95"]
    lines = [
        f"great_scale [{label}]: {snap['failures']}/{snap['runs']} failed  p={snap['p']:.6g}",
        f"S = heat(p) = p(1-p) = {snap['S']:.6g}",
        f"4S = {snap['logCoord']:.6g}   Psi = CONE + S = {snap['psi']:.6f}   CONE = phi^-2 = {CONE:.6f}",
        f"cone floor: {'yes' if snap['onConeFloor'] else 'no'}   orientation: {snap['orientation']}",
        (
            f"95% rate still invisible after {runs} clean runs: p < {miss:.6g}"
            if miss is not None
            else f"runs for 95% detection of this p: {n95}"
        ),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(card(0, 168, "session lattice evidence"))
    print(card(168, 168, "orientation control: all fail"))
    gs = load_repo_greatscale()
    if gs is not None:
        peer = gs.evidence_snapshot(0, 168)
        local = evidence_snapshot(0, 168)
        same = abs(peer["S"] - local["S"]) < 1e-15 and peer["orientation"] == local["orientation"]
        print(f"parity with avrone.greatscale: {'yes' if same else 'NO'}")
    else:
        print("parity with avrone.greatscale: module not on disk")
    print(json.dumps(evidence_snapshot(0, 168), indent=2))
