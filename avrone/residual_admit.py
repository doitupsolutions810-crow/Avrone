"""Signed residual admission. Peers greatscale / residual_gate. Does not replace them.

Heat is orientation-blind: S=heat(0)=heat(1)=0. Cone compliance is not correctness.
This module rejects:
  - orientation == "fail" (all runs failed), even when S == 0
  - a bound that was supplied but is not finite (NaN, +inf, -inf)
  - failures > runs, negative counts, or runs < 1 when evidence is requested
"""
from __future__ import annotations

import math
import os
from typing import Any, Optional

PHI = (1 + math.sqrt(5)) / 2
CONE = 1 / (PHI * PHI)


def heat(h: float) -> float:
    x = max(0.0, min(1.0, float(h)))
    return x * (1.0 - x)


def gate_on() -> bool:
    if os.environ.get("RESIDUAL_LOG_ONLY", "0") == "1":
        return False
    g = os.environ.get("AVRONE_RESIDUAL_GATE") or os.environ.get("RESIDUAL_GATE") or "0"
    return g in ("1", "true", "True")


def admit(
    *,
    failures: Optional[int] = None,
    runs: Optional[int] = None,
    S: Optional[float] = None,
    orientation: Optional[str] = None,
    bound: Optional[float] = None,
    tol: float = 1e-3,
) -> dict[str, Any]:
    reasons: list[str] = []
    if runs is not None or failures is not None:
        if runs is None or failures is None or runs < 1 or failures < 0 or failures > runs:
            reasons.append("evidence_domain")
            p = 1.0
            orient = "fail"
        else:
            p = failures / runs
            orient = "pass" if failures == 0 else ("fail" if failures == runs else "mixed")
        s = heat(p)
    elif S is not None and math.isfinite(S):
        s = max(0.0, min(0.25, float(S)))
        orient = orientation or "pass"
        p = None
    else:
        if S is not None and not math.isfinite(S):
            reasons.append("nonfinite_S")
        s = heat(0.5)
        orient = orientation or "pass"
        p = None

    if orientation in ("pass", "fail", "mixed"):
        orient = orientation

    if orient == "fail":
        reasons.append("orientation_fail")

    if bound is not None and not math.isfinite(bound):
        reasons.append("nonfinite_bound")
    elif bound is not None and bound > (tol if tol > 0 else 1e-3):
        reasons.append("bound_over_tol")

    if not (math.isfinite(CONE + s) and (CONE + s) >= CONE - 1e-12):
        reasons.append("cone")

    accepted = len(reasons) == 0
    action = "lead.rejected" if gate_on() and not accepted else "lead.merge"
    return {
        "S": s,
        "Psi": CONE + s,
        "fourST": 4 * s,
        "CONE": CONE,
        "p": p,
        "orientation": orient,
        "accepted": accepted,
        "reasons": reasons,
        "action": action,
        "gate": gate_on(),
    }
