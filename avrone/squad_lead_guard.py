"""Squad Lead residual guard.

Counts worker results the same way Trinity's ``_reviewer_failed`` does
(empty, or a line matching ``[name failed: ...]``), then calls signed admit.

``None`` is a missing result and counts as a failure before ``str()``.
The text ``"None"`` is a real report and does not.

This module does not edit trinity.py.
"""
from __future__ import annotations

import re
from typing import Any

from .residual_admit import admit

_FAILED_RE = re.compile(r"^\[[^\]\n]*? failed: ")


def worker_failed(result: Any) -> bool:
    if result is None:
        return True
    r = str(result).strip()
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
    n_fail = sum(1 for r in results if worker_failed(r))
    snap = admit(failures=n_fail, runs=n)
    snap["workers"] = n
    snap["worker_failures"] = n_fail
    return snap
