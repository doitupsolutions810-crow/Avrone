"""Lead call-site wrapper. Trinity imports this; this file does not edit trinity.py.

    from avrone.lead_admit import lead_decision
    decision = lead_decision(failures=fails, runs=n, bound=curvature_bound)
    if decision["action"] == "lead.rejected":
        return decision
"""
from __future__ import annotations

from typing import Any, Optional

from .residual_admit import admit


def lead_decision(
    *,
    failures: Optional[int] = None,
    runs: Optional[int] = None,
    bound: Optional[float] = None,
    orientation: Optional[str] = None,
) -> dict[str, Any]:
    return admit(failures=failures, runs=runs, bound=bound, orientation=orientation)
