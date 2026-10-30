#!/usr/bin/env python3
"""Count unique, structurally valid evolve proposals accepted since a run began.

The local squad launcher uses this as a release gate. Historical, malformed or
non-finite timestamps must never satisfy the current run's two-change requirement.
This module is stdlib-only so it can run before or after the editable install.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


def count_fresh_accepted(rows: Any, started_at: float) -> int:
    """Count unique accepted proposal records with finite timestamps >= started_at."""
    try:
        started = float(started_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("start timestamp must be finite") from exc
    if not math.isfinite(started):
        raise ValueError("start timestamp must be finite")
    if not isinstance(rows, list):
        raise ValueError("accepted proposal report must be a JSON list")

    seen: set[str] = set()
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("pid")
        branch = row.get("branch")
        files = row.get("files")
        ts = row.get("ts")
        if not isinstance(pid, str) or not pid or pid in seen:
            continue
        if not isinstance(branch, str) or not branch:
            continue
        if not isinstance(files, list) or not files or not all(isinstance(p, str) and p for p in files):
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        try:
            finite = math.isfinite(ts)
        except (OverflowError, TypeError, ValueError):
            continue
        if not finite or ts < started:
            continue
        seen.add(pid)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: count_fresh_evolve_proposals.py REPORT.json START_EPOCH", file=sys.stderr)
        return 2
    report_path = Path(args[0])
    try:
        started = float(args[1])
        with report_path.open("r", encoding="utf-8") as stream:
            rows = json.load(stream)
        count = count_fresh_accepted(rows, started)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        print(f"invalid accepted proposal report: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
