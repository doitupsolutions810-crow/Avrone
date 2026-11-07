"""Signed residual admission. Peers greatscale / residual_gate. Does not replace them.

Heat is orientation-blind: S = heat(0) = heat(1) = 0.
Evidence counts own the orientation. A caller cannot stamp orientation="pass"
over an all-fail count. Disagreement is orientation_conflict and fails closed.
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


def _count(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


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
    evidence_orient: Optional[str] = None
    p: Optional[float] = None

    if runs is not None or failures is not None:
        f, n = _count(failures), _count(runs)
        if f is None or n is None or n < 1 or f < 0 or f > n:
            reasons.append("evidence_domain")
            p = 1.0
            evidence_orient = "fail"
        else:
            p = f / n
            evidence_orient = "pass" if f == 0 else ("fail" if f == n else "mixed")
        s = heat(p)
        orient = evidence_orient
    elif S is not None and isinstance(S, (int, float)) and not isinstance(S, bool) and math.isfinite(float(S)):
        s = max(0.0, min(0.25, float(S)))
        orient = orientation if orientation in ("pass", "fail", "mixed") else "pass"
    else:
        if S is not None:
            reasons.append("nonfinite_S")
        s = heat(0.5)
        orient = orientation if orientation in ("pass", "fail", "mixed") else "pass"

    if evidence_orient is not None and orientation in ("pass", "fail", "mixed") and orientation != evidence_orient:
        reasons.append("orientation_conflict")
        orient = evidence_orient

    if orient == "fail":
        reasons.append("orientation_fail")

    limit = 1e-3
    if isinstance(tol, (int, float)) and not isinstance(tol, bool) and math.isfinite(float(tol)) and float(tol) > 0:
        limit = float(tol)
    if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(float(bound))):
        reasons.append("nonfinite_bound")
    elif isinstance(bound, (int, float)) and not isinstance(bound, bool) and float(bound) > limit:
        reasons.append("bound_over_tol")

    if not (math.isfinite(CONE + s) and (CONE + s) >= CONE - 1e-12):
        reasons.append("cone")

    accepted = len(reasons) == 0
    action = "lead.rejected" if gate_on() and not accepted else "lead.merge"
    return {
        "S": s,
        "Psi": CONE + s,
        "fourST": 4.0 * s,
        "CONE": CONE,
        "p": p,
        "orientation": orient,
        "accepted": accepted,
        "reasons": reasons,
        "action": action,
        "gate": gate_on(),
    }
