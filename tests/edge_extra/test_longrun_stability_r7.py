"""Round 7: time/scheduling and long-run stability.

Governor over many simulated ticks, evolve cap/backoff parsing, repeated
reap-cap kills, and telemetry over many samples including out-of-order
timestamps. Bounded and deterministic.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.evolve import (
    DEFAULT_MAX_ITERS,
    DEFAULT_TIME_BUDGET_S,
    LONG_MAX_ITERS_CAP,
    MAX_ITERS_CAP,
    TIME_BUDGET_CAP_S,
    _checkpoint_candidates,
    _env_num,
    _safe_test_cmd,
    hard_caps,
    long_loops_allowed,
    loop_limits,
)
from avrone.governor import Governor
from avrone.sovereign import LocalRunner
from avrone.telemetry import telemetry, _iter_events


def _readers(**vals):
    defaults = {"load": 0.1, "mem_pct": 20.0, "disk_free_pct": 50.0, "temp_c": 40.0}
    defaults.update(vals)
    return {k: (lambda v=v: v) for k, v in defaults.items()}


def test_governor_many_ticks_throttle_count_is_monotonic_and_bounded() -> None:
    clock = [0.0]
    load = [0.1]

    def read_load():
        return load[0]

    g = Governor(
        8, min_parallel=1, ttl_s=0, clock=lambda: clock[0],
        readers={"load": read_load, "mem_pct": lambda: 20.0,
                 "disk_free_pct": lambda: 50.0, "temp_c": lambda: 40.0},
    )
    keys_before = set(g.readers)
    last_count = 0
    targets = []
    ticks = 400
    for i in range(ticks):
        # Oscillate: healthy, then over LOAD_HARD (2.0), then healthy again.
        load[0] = 0.1 if (i // 10) % 2 == 0 else 9.0
        clock[0] += 0.05
        t = g.target()
        targets.append(t)
        assert 1 <= t <= 8
        assert g.throttle_count >= last_count
        last_count = g.throttle_count
        assert set(g.readers) == keys_before
        # Cache is a single (ts, health) tuple, not an ever-growing log.
        assert g._cache is None or (isinstance(g._cache, tuple) and len(g._cache) == 2)
    assert g.throttle_count <= ticks
    # We did throttle on the hot windows and recovered on the cool ones.
    assert 1 in targets and 8 in targets
    assert g.throttle_count > 0
    state = g.state()
    json.dumps(state, allow_nan=False)
    assert state["throttle_count"] == g.throttle_count


def test_governor_steady_hot_load_does_not_grow_internal_state() -> None:
    g = Governor(8, min_parallel=1, ttl_s=0, readers=_readers(load=9.0))
    for _ in range(200):
        assert g.target() == 1
    assert g.throttle_count == 200
    assert len(g.readers) == 4
    assert g._cache is not None and len(g._cache) == 2


def test_evolve_loop_limits_junk_and_caps(monkeypatch) -> None:
    monkeypatch.delenv("AVRONE_EVOLVE_LONG", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_MAX_ITERS", raising=False)
    monkeypatch.delenv("AVRONE_EVOLVE_TIME_BUDGET", raising=False)
    assert long_loops_allowed() is False
    assert hard_caps() == (MAX_ITERS_CAP, TIME_BUDGET_CAP_S)
    n, b = loop_limits()
    assert n == DEFAULT_MAX_ITERS and b == DEFAULT_TIME_BUDGET_S

    # Whitespace / wrong spelling does not opt in.
    for raw in ("", " ", "ALLOW ", "yes", "1", "true", "nallow"):
        monkeypatch.setenv("AVRONE_EVOLVE_LONG", raw)
        # "ALLOW " after strip+lower is "allow" — that one SHOULD opt in.
        if raw.strip().lower() == "allow":
            assert long_loops_allowed() is True
        else:
            assert long_loops_allowed() is False

    monkeypatch.setenv("AVRONE_EVOLVE_LONG", " allow ")
    assert long_loops_allowed() is True
    assert hard_caps() == (LONG_MAX_ITERS_CAP, 7200)

    # Agent cannot exceed the hard cap, even if env and args ask for more.
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "9999")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "99999")
    n, b = loop_limits(max_iters=10**6, time_budget_s=10**9)
    assert n == LONG_MAX_ITERS_CAP and b == 7200

    # Junk env is ignored (falls back to hard/default).
    monkeypatch.setenv("AVRONE_EVOLVE_LONG", "off")
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_ITERS", "nope")
    monkeypatch.setenv("AVRONE_EVOLVE_TIME_BUDGET", "inf")
    assert _env_num("AVRONE_EVOLVE_MAX_ITERS") is None
    assert _env_num("AVRONE_EVOLVE_TIME_BUDGET") is None
    n, b = loop_limits(max_iters="nope", time_budget_s=float("nan"))
    assert n == DEFAULT_MAX_ITERS and b == DEFAULT_TIME_BUDGET_S


def test_evolve_checkpoint_candidates_rejects_oversized_and_bad() -> None:
    assert _checkpoint_candidates(None) is None
    assert _checkpoint_candidates([{"path": "a.py", "content": "x"}]) == [{"path": "a.py", "content": "x"}]
    # Non-JSON-encodable values are dropped, not raised.
    assert _checkpoint_candidates([{"fn": lambda: None}]) is None  # type: ignore[list-item]
    huge = ["y" * 1000] * 2000
    out = _checkpoint_candidates(huge)
    assert out is None


def test_safe_test_cmd_unclosed_quote_is_a_quoted_fallback() -> None:
    # shlex.split raises ValueError on an unclosed quote; the helper must not.
    out = _safe_test_cmd("pytest 'unclosed")
    assert isinstance(out, str) and out
    import shlex
    shlex.split(out)


@pytest.mark.asyncio
async def test_repeated_reap_cap_leaves_no_zombies(tmp_path: Path) -> None:
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 8
    r = LocalRunner(s)
    await r.start()
    for _ in range(6):
        res = await r.run("sleep 30", timeout=1)
        assert res.timed_out is True
    # Give the reaper a moment, then confirm we didn't accumulate children.
    await asyncio.sleep(0.05)
    try:
        import psutil
        kids = psutil.Process().children(recursive=True)
        live = [p for p in kids if p.status() != "zombie" and "sleep 30" in " ".join(p.cmdline())]
        assert live == [], live
    except ImportError:
        # psutil is optional; the reap itself must still have returned.
        pass


def test_telemetry_many_samples_and_out_of_order_ts(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    now = time.time()
    # Write a single log with 80 runs (some inverted end<start) plus poison lines.
    log = c.log_dir / "bulk.jsonl"
    lines = []
    for i in range(80):
        start = now - 1000 + i
        end = start - 5 if i % 7 == 0 else start + (i % 5) + 0.25
        lines.append(json.dumps({"ts": start, "kind": "run.start", "run_id": f"r{i}",
                                 "data": {"mode": "chat", "prompt": f"p{i}"}}))
        lines.append(json.dumps({"ts": end, "kind": "run.done", "run_id": f"r{i}", "data": {}}))
    lines.insert(0, "[1, 2, 3]")
    lines.insert(1, json.dumps({"ts": "later", "kind": "run.start", "run_id": "bad", "data": {}}))
    lines.append("not-json")
    log.write_text("\n".join(lines) + "\n")

    evs = list(_iter_events(c.log_dir, now - 10_000))
    # Poison lines are skipped; 80 start + 80 done remain.
    assert len(evs) == 160
    # Timestamps that made it through are finite numbers (not necessarily sorted
    # — a log may be out of order; the aggregator must still be stable).
    stamps = [e["ts"] for e in evs]
    assert all(isinstance(t, (int, float)) and math.isfinite(t) for t in stamps)

    core = SimpleNamespace(
        codex=c, active={}, fleet=None,
        governor=Governor(4, enabled=False, ttl_s=60, readers=_readers()),
        settings=Settings(),
    )
    payload = telemetry(core, window_s=1e9)
    raw = json.dumps(payload, allow_nan=False)
    assert "NaN" not in raw and "Infinity" not in raw
    runs = payload["runs"]
    assert runs["total"] == 80 and runs["completed"] == 80
    # Percentiles are non-negative even when some end times precede start.
    assert runs["p50_s"] is not None and runs["p50_s"] >= 0
    assert runs["p95_s"] is not None and runs["p95_s"] >= 0
    assert runs["p95_s"] >= runs["p50_s"]
