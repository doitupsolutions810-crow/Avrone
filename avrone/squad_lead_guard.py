"""Squad Lead residual guard.

Only an exact list or an exact tuple is a squad. Subclasses are rejected
without calling ``len`` or iterating.

Workers are snapshotted (a shallow copy) before any ``str``. Inserts,
appends, and deletions on the original container are not part of the score.
The copy is the evidence set.

``None``, blank text, ``[name failed: ...]``, and a ``__str__`` that raises
count as worker failures. The text ``"None"`` does not.

A structural reject is unmeasured: S, Psi, and fourST are None.

This module does not edit trinity.py.
"""
from __future__ import annotations

import re
from typing import Any

from .residual_admit import admit

_FAILED_RE = re.compile(r"^\[[^\]\n]*? failed: ")


def _reject(reason: str) -> dict[str, Any]:
    snap = admit()
    snap["reasons"] = [reason]
    snap["accepted"] = False
    snap["measured"] = False
    snap["S"] = None
    snap["Psi"] = None
    snap["fourST"] = None
    snap["p"] = None
    snap["orientation"] = "unmeasured"
    if snap["gate"]:
        snap["action"] = "lead.rejected"
    else:
        snap["action"] = "lead.merge"
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
    if type(results) is not list and type(results) is not tuple:
        return _reject("not_a_squad")
    items = list(results)
    n = len(items)
    if n < 1:
        return _reject("no_workers")
    n_fail = sum(1 for item in items if worker_failed(item))
    snap = admit(failures=n_fail, runs=n)
    snap["measured"] = True
    snap["workers"] = n
    snap["worker_failures"] = n_fail
    return snap
