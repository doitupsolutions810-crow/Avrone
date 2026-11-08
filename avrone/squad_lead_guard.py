"""Squad Lead residual guard.

Counts worker results the same way Trinity's ``_reviewer_failed`` does
(empty, or a line matching ``[name failed: ...]``), then calls signed admit.

Trinity call site, immediately before ``codex.emit(..., "lead.merge" ...)``::

    from .squad_lead_guard import guard_squad
    residual = guard_squad(results)
    self.codex.emit(run_id, "residual.snapshot", "Residual",
                    S=residual["S"], orientation=residual["orientation"],
                    accepted=residual["accepted"], action=residual["action"])
    if residual["action"] == "lead.rejected":
        return {"run_id": run_id, "answer": "Lead rejected by residual gate", "residual": residual}

This module does not edit trinity.py.
"""
from __future__ import annotations

import re
from typing import Any

from .residual_admit import admit

_FAILED_RE = re.compile(r"^\[[^\]\n]*? failed: ")


def worker_failed(result: str) -> bool:
    r = (result or "").strip()
    return not r or bool(_FAILED_RE.match(r))


def guard_squad(results: list[Any]) -> dict[str, Any]:
    n = len(results)
    if n < 1:
        snap = admit()
        snap["reasons"] = ["no_workers", *snap["reasons"]]
        snap["accepted"] = False
        if snap["gate"]:
            snap["action"] = "lead.rejected"
        return snap
    n_fail = sum(1 for r in results if worker_failed(str(r)))
    snap = admit(failures=n_fail, runs=n)
    snap["workers"] = n
    snap["worker_failures"] = n_fail
    return snap
