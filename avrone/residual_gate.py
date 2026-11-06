"""
Residual gate policy — Descent CIV / XCV fail-closed admission.
Peers avrone.greatscale (shared CONE, heat, psi) without replacing it.

Env:
  AVRONE_RESIDUAL_GATE=1 | RESIDUAL_GATE=1  → fail-closed when not accepted
  RESIDUAL_LOG_ONLY=1                        → never veto (log path only)
  RESIDUAL_QUADRATURE_TOLERANCE              → bound tolerance (default 1e-3)
"""
from __future__ import annotations

import math
import os
from typing import Any, Optional

try:
    from .greatscale import CONE, heat, psi, cone_compliant, log_coordinate, evidence_snapshot
except ImportError:  # standalone / tests
    PHI = (1 + math.sqrt(5)) / 2
    CONE = 1 / (PHI ** 2)

    def heat(h: float) -> float:
        x = max(0.0, min(1.0, float(h)))
        return x * (1.0 - x)

    def psi(s: float) -> float:
        return CONE + max(0.0, float(s))

    def cone_compliant(s: float) -> bool:
        return psi(s) >= CONE - 1e-12

    def log_coordinate(s: float) -> float:
        return 4.0 * max(0.0, float(s))

    def evidence_snapshot(failures: int, runs: int, alpha: float = 0.05) -> dict[str, Any]:
        p = failures / runs if runs else 0.0
        s = heat(p)
        return {"S": s, "psi": psi(s), "p": p, "failures": failures, "runs": runs}


def residual_gate_enabled() -> bool:
    if os.environ.get("RESIDUAL_LOG_ONLY", "0") == "1":
        return False
    g = os.environ.get("AVRONE_RESIDUAL_GATE") or os.environ.get("RESIDUAL_GATE") or "0"
    return g in ("1", "true", "True")


def residual_snapshot(
    S: Optional[float] = None,
    chi: Optional[float] = None,
    e: Optional[float] = None,
    rigorous_curvature_error_bound: Optional[float] = None,
    rigorous_metric_area_error_bound: Optional[float] = None,
    quadrature_tolerance: Optional[float] = None,
    failures: Optional[int] = None,
    runs: Optional[int] = None,
) -> dict[str, Any]:
    """Unified residual snapshot for Lead admission / CodeX emit."""
    tol = quadrature_tolerance
    if tol is None:
        try:
            tol = float(os.environ.get("RESIDUAL_QUADRATURE_TOLERANCE", "0.001"))
        except ValueError:
            tol = 1e-3
    if tol <= 0:
        tol = 1e-3

    if failures is not None and runs is not None and runs > 0:
        ev = evidence_snapshot(int(failures), int(runs))
        s = float(ev["S"])
        orientation = ev.get("orientation", "mixed")
    elif S is not None and math.isfinite(S):
        s = max(0.0, min(0.25, float(S)))
        orientation = "pass"
    elif chi is not None and math.isfinite(chi):
        c = max(0.0, min(1.0, float(chi)))
        s = heat((1.0 + c) / 2.0)
        orientation = "pass"
    elif e is not None and math.isfinite(e):
        ee = max(0.0, min(1.0, float(e)))
        s = ee / 2.0 - (ee * ee) / 4.0
        orientation = "pass"
    else:
        s = heat(0.5)
        orientation = "pass"

    Psi = psi(s)
    four_st = log_coordinate(s)
    compliant = cone_compliant(s)

    has_curv = rigorous_curvature_error_bound is not None and math.isfinite(rigorous_curvature_error_bound)
    has_area = rigorous_metric_area_error_bound is not None and math.isfinite(rigorous_metric_area_error_bound)
    bounds_ok = True
    if has_curv and (rigorous_curvature_error_bound or 0) > tol:
        bounds_ok = False
    if has_area and (rigorous_metric_area_error_bound or 0) > tol:
        bounds_ok = False

    accepted = compliant and bounds_ok
    return {
        "S": s,
        "Psi": Psi,
        "fourST": four_st,
        "coneCompliant": compliant,
        "rigorousBoundFinite": has_curv or has_area,
        "accepted": accepted,
        "CONE": CONE,
        "orientation": orientation,
        "peer": "greatscale",
    }


def admit_lead(snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fail-closed Lead admission when residual gate is ON."""
    snap = snapshot or residual_snapshot()
    if residual_gate_enabled() and not snap.get("accepted", True):
        return {"ok": False, "action": "lead.rejected", "residual": snap, "reason": "coneCompliant_or_bound_failed"}
    return {"ok": True, "action": "lead.merge", "residual": snap}
