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

# Only EVENT LINES count. AVRONE prints one event per line as "[run_id] kind  agent  detail", and it
# flattens tool output and agent text onto that line. So "tool.call x port_scan" inside a tool.result
# (an agent cat-ing a log or test file, or echoing it) or inside agent.done narration must never count
# as a call. Likewise "no unknown tool errors" in narration is not an unknown-tool error.
_PFX = r"^[ \t]*(?:\[[^\]\s]*\]|\d{1,2}:\d{2}(?::\d{2})?)?[ \t]*"
CALL = re.compile(_PFX + r"tool\.call[ \t]+\S+[ \t]+([A-Za-z0-9_]+)", re.M)
MALFORMED = re.compile(_PFX + r"action\.malformed\b", re.M)
EVENT = re.compile(_PFX + r"([a-z]+\.[a-z_.]+)[ \t]", re.M)
UNKNOWN = re.compile(r"unknown tool", re.I)
BACKEND = re.compile(r"Model backend error (5\d\d)")
MIN_FRACTION = 0.6
MAX_BAD = 3


def judge(log: str, nxt: list[str]) -> tuple[bool, list[str], int]:
    nxt = [t for t in dict.fromkeys(t.strip() for t in nxt) if t]
    if not nxt:
        return False, [], 0
    called = set(CALL.findall(log))
    ok = [t for t in nxt if t in called]
    bad = len(MALFORMED.findall(log)) + sum(1 for line in log.splitlines() if _unknown_line(line))
    return (len(ok) / len(nxt) >= MIN_FRACTION and bad <= MAX_BAD), ok, bad


def _unknown_line(line: str) -> bool:
    """An unknown-tool error is a tool.result/error event, or a bare (non-event) line; never narration."""
    if not UNKNOWN.search(line):
        return False
    m = EVENT.match(line)
    return m is None or m.group(1) in ("tool.result", "error")


def backend_errors(log: str) -> int:
    """5xx answers from the model server (e.g. Ollama failing to parse a gpt-oss tool call)."""
    return len(BACKEND.findall(log))


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
    be = backend_errors(log)
    if be and not passed:
        print(f"  smoke: HINT the model server answered 5xx {be}x — the model/server is failing (e.g. Ollama "
              f"can't parse the model's tool call), not the tools; check {errlog} and ollama.log")
    if passed:
        try:
            new = str(int(stage_s) + 1)
        except ValueError:
            return 0
        try:
            _atomic_write(stage_file, new)
            _atomic_write(stage_file + ".bak", new)
        except OSError as e:
            print(f"  smoke: WARN could not save stage {new}: {e.strerror or e} (will retry next cycle)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
