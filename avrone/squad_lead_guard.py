"""Squad Lead residual guard.

Counts worker results the same way Trinity's ``_reviewer_failed`` does
(empty, or a line matching ``[name failed: ...]``), then calls signed admit.

``None`` is a missing result and counts as a failure before ``str()``.
The text ``"None"`` is a real report and does not.
A ``__str__`` that raises counts as a failure (fail closed, no crash).
A string or bytes object is not a squad.

This module does not edit trinity.py.
"""
from __future__ import annotations

import re
from typing import Any

from .residual_admit import admit

_FAILED_RE = re.compile(r"^\[[^\]\n]*? failed: ")


def _reject(reason: str) -> dict[str, Any]:
    snap = admit()
    snap["reasons"] = [reason, *snap["reasons"]]
    snap["accepted"] = False
    if snap["gate"]:
        snap["action"] = "lead.rejected"
    return snap


def worker_failed(result: Any) -> bool:
    if result is None:
        return True
    try:
        text = str(result)
    except Exception:
        return True
    if not isinstance(text, str):
        return True
    r = text.strip()
    return not r or bool(_FAILED_RE.match(r))


def guard_squad(results: Any) -> dict[str, Any]:
    if isinstance(results, (str, bytes, bytearray)):
        return _reject("not_a_squad")
    try:
        items = list(results)
    except TypeError:
        return _reject("not_a_squad")
    n = len(items)
    if n < 1:
        return _reject("no_workers")
    n_fail = sum(1 for r in items if worker_failed(r))
    snap = admit(failures=n_fail, runs=n)
    snap["workers"] = n
    snap["worker_failures"] = n_fail
    return snap
