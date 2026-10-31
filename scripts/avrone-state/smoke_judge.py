#!/usr/bin/env python3
"""Judge one reintro smoke run and advance the stage when the batch was exercised coherently.

usage: smoke_judge.py <smoke.err> <comma-separated next tools> <stage file> <current stage>

ADVANCE when >= 60% of the batch was called and there were <= 3 malformed/unknown calls.
On advance, writes stage+1 atomically to the stage file and to <stage file>.bak (the .bak lets
the autopilot recover progress if the stage file is later deleted or corrupted).
Never raises: a missing/unreadable/garbage log is a HOLD. Exit 0 always; prints one line.
"""
from __future__ import annotations

import os
import re
import sys

CALL = re.compile(r"tool\.call\s+\S+\s+([A-Za-z0-9_]+)")
UNKNOWN = re.compile(r"unknown tool", re.I)
MIN_FRACTION = 0.6
MAX_BAD = 3


def judge(log: str, nxt: list[str]) -> tuple[bool, list[str], int]:
    nxt = [t for t in dict.fromkeys(t.strip() for t in nxt) if t]
    if not nxt:
        return False, [], 0
    called = set(CALL.findall(log))
    ok = [t for t in nxt if t in called]
    bad = log.count("action.malformed") + len(UNKNOWN.findall(log))
    return (len(ok) / len(nxt) >= MIN_FRACTION and bad <= MAX_BAD), ok, bad


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("  smoke: usage error -> HOLD")
        return 0
    errlog, nxt_s, stage_file, stage_s = argv[1:]
    try:
        with open(errlog, "rb") as f:
            log = f.read(50_000_000).decode("utf-8", errors="replace")
    except OSError:
        log = ""
    nxt = nxt_s.split(",") if nxt_s and nxt_s != "-" else []
    passed, ok, bad = judge(log, nxt)
    missed = sorted(set(t for t in nxt if t) - set(ok))
    n = len([t for t in dict.fromkeys(nxt) if t])
    print(f"  smoke: {len(ok)}/{n} exercised, malformed/unknown={bad} -> {'ADVANCE' if passed else 'HOLD'}; missed={missed}")
    if passed:
        try:
            new = str(int(stage_s) + 1)
        except ValueError:
            return 0
        _atomic_write(stage_file, new)
        _atomic_write(stage_file + ".bak", new)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
