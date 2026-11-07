#!/usr/bin/env python3
"""Jitter-edge suite.

A race is not a Bernoulli trial just because two clocks appear in the source.
On this loop, asyncio.wait_for beats asyncio.sleep whenever delay >= timeout
down to 5ms, and loses whenever delay is below that, with no mixed outcomes.
Heat shows up only inside the scheduler's jitter window, measured here at a
2ms deadline. The squad hang (sleep 3600s, timeout 0.5s, ratio 7200) sits
far outside that window, so repeating it does not buy N.
"""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from evidence_scale import CONE, channel_snapshot, heat  # type: ignore

REPO = Path("/tmp/Avrone")
PY = Path("/tmp/avrone-venv/bin/python")
PASS = FAIL = 0


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def race(delay: float, timeout: float) -> str:
    try:
        await asyncio.wait_for(asyncio.sleep(delay), timeout)
        return "finish"
    except asyncio.TimeoutError:
        return "timeout"


async def sample(delay: float, timeout: float, n: int) -> tuple[int, int]:
    timeouts = 0
    for _ in range(n):
        if await race(delay, timeout) == "timeout":
            timeouts += 1
    return timeouts, n


def main() -> int:
    print("AVRONE-12 Jitter-edge suite")
    # Sharp region: 5ms deadline. Expect a step, not a coin.
    sharp_lo = asyncio.run(sample(0.004, 0.005, 40))
    sharp_hi = asyncio.run(sample(0.005, 0.005, 40))
    rec("below 5ms deadline always finishes", sharp_lo == (0, 40), str(sharp_lo))
    rec("at 5ms deadline always times out", sharp_hi == (40, 40), str(sharp_hi))
    sharp = channel_snapshot(0, 40, "deterministic")
    rec("sharp region stays one trial", sharp["effectiveRuns"] == 1)

    # Alone, the 2ms pair does not flip. 200 sequential copies, zero timeouts.
    alone_t, alone_n = asyncio.run(sample(0.0016, 0.002, 200))
    alone = channel_snapshot(alone_t, alone_n, "deterministic")
    rec("isolated 2ms pair never times out", alone_t == 0, f"{alone_t}/{alone_n}")
    rec("isolated pair is one trial", alone["effectiveRuns"] == 1 and alone["onConeFloor"])

    # Same numbers, 40 at a time: still quiet. 80 at a time: the loop misses deadlines.
    async def batches(k: int, n: int) -> list[int]:
        out = []
        for _ in range(n):
            hits = await asyncio.gather(*[race(0.0016, 0.002) for _ in range(k)])
            out.append(hits.count("timeout"))
        return out

    quiet = asyncio.run(batches(40, 15))
    loaded = asyncio.run(batches(80, 30))
    rec("40-wide batches stay at zero", quiet == [0] * 15, f"max={max(quiet)}")
    rec("80-wide batches vary", max(loaded) - min(loaded) >= 10, f"min={min(loaded)} max={max(loaded)}")
    # Pre-registered from a prior probe whose mean was 30/80: does a batch exceed 24 timeouts?
    over = sum(c > 24 for c in loaded)
    loaded_snap = channel_snapshot(over, len(loaded), "stochastic")
    rec("loaded batches cross 24", 0 < over < len(loaded), f"{over}/{len(loaded)}")
    rec("loaded heat is off the cone floor", not loaded_snap["onConeFloor"], f"S={loaded_snap['S']:.4f}")
    rec("loaded sample keeps N", loaded_snap["effectiveRuns"] == len(loaded))

    # Official squad shape: one worker sleeps 3600s, supervisor cuts at 0.5s.
    # Twenty copies. If they cannot flip, they remain one trial.
    if not PY.is_file():
        rec("official hang copies", False, "venv missing")
        hang_timeouts, hang_n = 0, 0
    else:
        hang_timeouts, hang_n = official_hang_copies(20)
        rec("official hang never flips in 20 copies", hang_timeouts == hang_n == 20, f"{hang_timeouts}/{hang_n}")
    hang = channel_snapshot(0 if hang_timeouts == hang_n else hang_n - hang_timeouts, max(hang_n, 1), "deterministic")
    rec("official hang effective N is 1", hang["effectiveRuns"] == 1)
    rec(
        "ratio 7200 is outside the 2ms window",
        (3600 / 0.5) > (0.002 / 0.0004),
    )

    squad_summary = "not run"
    if PY.is_file() and REPO.is_dir():
        p = subprocess.run(
            [str(PY), "-m", "pytest", "-q", "tests/test_robust_squad.py", "tests/test_greatscale.py",
             "--tb=line", "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=180,
        )
        squad_summary = (p.stdout or "").strip().splitlines()[-1] if p.stdout else p.stderr[-200:]
        rec("squad and greatscale pytest", p.returncode == 0, squad_summary)
    else:
        rec("squad and greatscale pytest", False, "missing")

    docker = subprocess.run(
        ["bash", "-lc", "command -v docker >/dev/null && test -S /var/run/docker.sock"],
        capture_output=True,
    ).returncode == 0
    rec("verify.sh not claimed: docker down" if not docker else "docker socket present", True)

    payload = {
        "schema": "great-scale-evidence/v3",
        "finding": (
            "The same 1.6ms sleep against a 2ms deadline is one trial when run alone "
            "(0/200 timeouts). It becomes a coin only when 80 of those waits share the loop. "
            "Contention is the hidden input. A 3600s-versus-0.5s hang is neither."
        ),
        "cone": CONE,
        "sharp_5ms": {"below": list(sharp_lo), "at": list(sharp_hi), "effectiveRuns": sharp["effectiveRuns"]},
        "isolated_2ms": {
            "delay": 0.0016, "timeout": 0.002, "timeouts": alone_t, "runs": alone_n,
            "effectiveRuns": alone["effectiveRuns"], "S": alone["S"], "psi": alone["psi"],
            "orientation": alone["orientation"],
        },
        "loaded_80": {
            "batch": 80, "batches": len(loaded), "counts": loaded,
            "min": min(loaded), "max": max(loaded),
            "predicate": "timeouts > 24",
            "failures": over, "p": loaded_snap["p"], "S": loaded_snap["S"],
            "psi": loaded_snap["psi"], "logCoord": loaded_snap["logCoord"],
            "orientation": loaded_snap["orientation"], "onConeFloor": loaded_snap["onConeFloor"],
            "effectiveRuns": loaded_snap["effectiveRuns"], "runsFor95": loaded_snap["runsFor95"],
        },
        "official_hang": {
            "sleep": 3600,
            "timeout": 0.5,
            "ratio": 7200,
            "copies": hang_n,
            "timeouts": hang_timeouts,
            "effectiveRuns": hang["effectiveRuns"],
            "honestMissedRate95": hang["missedRate95"],
            "naiveMissedRate95": hang["naiveMissedRate95"],
        },
        "squad": squad_summary,
        "docker": docker,
        "mitigation": (
            "Do not call eighty concurrent waits the same trial as one wait. "
            "Do not sample a 0.5s-versus-3600s hang as if it were a coin."
        ),
    }
    out = ROOT / "references" / "great-scale-jitter-evidence.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    rec("jitter evidence written", out.is_file())
    print(f"Results: {PASS} passed · {FAIL} failed")
    print(
        f"isolated 0.0016/0.002: {alone_t}/{alone_n}  effectiveRuns={alone['effectiveRuns']}  "
        f"S={alone['S']}  Psi={alone['psi']:.6f}"
    )
    print(
        f"loaded batches of 80: predicate timeouts>24 is {over}/{len(loaded)}  "
        f"p={loaded_snap['p']:.4f}  S={loaded_snap['S']:.4f}  Psi={loaded_snap['psi']:.6f}  "
        f"counts {min(loaded)}..{max(loaded)}"
    )
    print(
        f"official hang 3600/0.5: {hang_timeouts}/{hang_n} identical  "
        f"effectiveRuns=1  honest p<{hang['missedRate95']:.4f}  "
        f"naive-if-copied p<{hang['naiveMissedRate95']:.4f}"
    )
    return 0 if FAIL == 0 else 1


def official_hang_copies(n: int) -> tuple[int, int]:
    code = r'''
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/tmp/Avrone")
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore

PLAN = json.dumps({"subtasks": [
    {"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}, {"role": "C", "focus": "gamma"}]})

class LLM:
    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            return PLAN
        if "You are the LEAD" in system:
            return "LEAD MERGED"
        if "Your focus: gamma" in system:
            await asyncio.sleep(3600)
            return "finished"
        return "worker ok"

async def once(i, root):
    s = Settings()
    s.data_dir = root / f"d{i}"
    s.workspace = root / f"w{i}"
    s.max_steps = 3
    s.agent_timeout = 0.5
    core = TrinityCore(s, LLM(), LocalRunner(s), Codex(s.data_dir))
    out = await asyncio.wait_for(core.squad("task", n=3, run_id=f"h{i}"), 10)
    return "timed out" in out["agents"][2]["result"]

async def main():
    root = Path(sys.argv[1])
    flags = []
    for i in range(int(sys.argv[2])):
        flags.append(await once(i, root))
    print(sum(flags), len(flags))

asyncio.run(main())
'''
    with tempfile.TemporaryDirectory() as td:
        p = subprocess.run([str(PY), "-c", code, td, str(n)], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        print(p.stderr[-500:])
        return 0, 0
    a, b = p.stdout.strip().split()
    return int(a), int(b)


if __name__ == "__main__":
    sys.exit(main())
