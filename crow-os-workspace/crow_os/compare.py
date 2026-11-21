"""Side-by-side round comparison. Outcomes come only from stored results."""

from __future__ import annotations

from typing import Any

FAILING = {"failed", "error"}


def _outcome_map(rnd: dict[str, Any]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for case in rnd.get("cases") or []:
        file_path = case.get("file") or case.get("file_path") or ""
        name = case.get("name") or case.get("case_name") or ""
        if not file_path and not name:
            continue
        mapped[f"{file_path}::{name}"] = (case.get("outcome") or "").lower()
    if mapped:
        return mapped
    for item in rnd.get("files") or []:
        path = item.get("path") or ""
        if path:
            mapped[path] = (item.get("status") or "").lower()
    return mapped


def compare_rounds(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Compare round `left` (older / A) with round `right` (newer / B)."""
    a = _outcome_map(left)
    b = _outcome_map(right)
    keys = sorted(set(a) | set(b))
    rows = []
    fixed = []
    regressed = []
    new_failures = []
    still_failing = []
    for key in keys:
        outcome_a = a.get(key)
        outcome_b = b.get(key)
        if outcome_a in FAILING and outcome_b == "passed":
            delta = "fixed"
            fixed.append(key)
        elif outcome_a == "passed" and outcome_b in FAILING:
            delta = "regressed"
            regressed.append(key)
        elif outcome_a is None and outcome_b in FAILING:
            delta = "new_failure"
            new_failures.append(key)
        elif outcome_a in FAILING and outcome_b in FAILING:
            delta = "still_failing"
            still_failing.append(key)
        elif outcome_a == outcome_b:
            delta = "unchanged"
        elif outcome_b is None:
            delta = "only_in_a"
        else:
            delta = "changed"
        rows.append({"key": key, "a": outcome_a, "b": outcome_b, "delta": delta})

    def _count(mapping: dict[str, str], names: set[str]) -> int:
        return sum(1 for value in mapping.values() if value in names)

    return {
        "round_a": left.get("id"),
        "round_b": right.get("id"),
        "commit_a": left.get("commit_sha"),
        "commit_b": right.get("commit_sha"),
        "pass_a": _count(a, {"passed"}),
        "pass_b": _count(b, {"passed"}),
        "fail_a": _count(a, FAILING),
        "fail_b": _count(b, FAILING),
        "pass_delta": _count(b, {"passed"}) - _count(a, {"passed"}),
        "fail_delta": _count(b, FAILING) - _count(a, FAILING),
        "fixed": fixed,
        "regressed": regressed,
        "new_failures": new_failures,
        "still_failing": still_failing,
        "rows": rows,
    }
