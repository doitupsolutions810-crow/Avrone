"""Regressions for honest fleet bridge merge counts and hostile health integers.

Direct Fleet helper integration is intentional: tests exercise real Bridge
merge_remote semantics as well as malformed callbacks from plugin peers.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import fleet as fm


class StubBridge:
    def __init__(self, merged=0, throw=False):
        self.merged = merged
        self.throw = throw
        self.rows = None

    def export(self, since=None):
        return []

    def merge_remote(self, rows, origin):
        self.rows = rows
        if self.throw:
            raise RuntimeError("simulated peer merger failure")
        return {"merged": self.merged, "skipped": len(rows) - self.merged}


class HostileInt(int):
    def __lt__(self, other):
        raise RuntimeError("hostile less-than")

    def __gt__(self, other):
        raise RuntimeError("hostile greater-than")

    def __int__(self):
        raise RuntimeError("hostile __int__")


@pytest.mark.parametrize("reported,expected", [
    (0, 0), (1, 1), (2, 2), (5, 2), (-1, 0), (-200, 0),
    (True, 0), (False, 0), (3.0, 0), (None, 0), ("3", 0),
    (10 ** 500, 2), (HostileInt(2), 2),
])
def test_bridge_merge_counts_only_actual_bounded_admissions(reported, expected):
    bridge = StubBridge(merged=reported)
    rows = [{"id": "one"}, {"id": "two"}]
    assert fm.bridge_merge(SimpleNamespace(bridge=bridge), rows, "peer") == expected
    assert bridge.rows == rows


def test_bridge_merge_without_bridge_is_zero():
    assert fm.bridge_merge(SimpleNamespace(bridge=None), [{"id": "a"}], "peer") == 0


def test_bridge_merge_with_no_valid_records_is_zero():
    bridge = StubBridge(merged=9)
    assert fm.bridge_merge(SimpleNamespace(bridge=bridge),
                           [42, {"x": float("nan")}], "peer") == 0
    assert bridge.rows is None


def test_bridge_merge_failed_peer_handler_returns_zero():
    bridge = StubBridge(merged=1, throw=True)
    assert fm.bridge_merge(SimpleNamespace(bridge=bridge),
                           [{"id": "ok"}], "peer") == 0


def test_bridge_merge_limits_large_batch_before_accounting():
    bridge = StubBridge(merged=1000000)
    rows = [{"id": str(i)} for i in range(5000)]
    assert fm.bridge_merge(SimpleNamespace(bridge=bridge),
                           rows, "peer") == fm.BRIDGE_MAX_POSTS
    assert bridge.rows[0]["id"] == "4950"


def _record(pid, kind="finding"):
    return {"id": pid, "kind": kind, "text": f"event {pid}",
            "agent": "peer", "refs": [], "ts": 1.0}


def test_actual_bridge_duplicate_replay_is_not_double_counted(tmp_path):
    bridge = bm.Bridge("merge-admission", tmp_path)
    core = SimpleNamespace(bridge=bridge)
    rows = [_record("first"), _record("second")]
    assert fm.bridge_merge(core, rows, "peer") == 2
    assert fm.bridge_merge(core, rows, "peer") == 0
    assert fm.bridge_merge(core, [rows[0], _record("third")], "peer") == 1
    assert [p["id"] for p in bm.Bridge("merge-admission", tmp_path).posts()] == [
        "first", "second", "third"
    ]


def test_actual_bridge_rejected_row_does_not_inflate_count(tmp_path):
    bridge = bm.Bridge("merge-reject", tmp_path)
    core = SimpleNamespace(bridge=bridge)
    assert fm.bridge_merge(core, [
        _record("one"), _record("bad-kind", "invalid"),
        _record("two"),
    ], "peer") == 2
    assert [p["id"] for p in bridge.posts()] == ["one", "two"]


@pytest.mark.parametrize("value,expected", [
    (HostileInt(-1), 0), (HostileInt(0), 0),
    (HostileInt(1), 1), (HostileInt(10000), 10000),
    (HostileInt(10001), 10000), (HostileInt(10 ** 1000), 10000),
    (True, None), (None, None),
])
def test_hostile_int_telemetry_is_bounded(value, expected):
    health = fm.sanitize_health({"state": "hot", "active_agents": value})
    assert health["state"] == "hot"
    if expected is None:
        assert "active_agents" not in health
    else:
        assert health["active_agents"] == expected


def test_hostile_int_does_not_erase_other_health_fields():
    assert fm.sanitize_health({
        "active_agents": HostileInt(15), "mem_pct": 52.5, "load": 3.2
    }) == {"active_agents": 15, "mem_pct": 52.5, "load": 3.2}


def test_500_seeded_admission_accounting_scenarios():
    rng = random.Random(2651115)
    for _ in range(500):
        count = rng.randint(0, 65)
        claimed = rng.choice([
            rng.randint(-100, 100), True, "3", None, rng.random()
        ])
        handler = StubBridge(merged=claimed)
        actual = fm.bridge_merge(
            SimpleNamespace(bridge=handler),
            [{"id": str(i)} for i in range(count)], "peer")
        assert type(actual) is int
        assert 0 <= actual <= min(count, fm.BRIDGE_MAX_POSTS)
