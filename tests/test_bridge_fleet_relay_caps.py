"""Adversarial fleet relay budget and telemetry regressions.

Direct in-process callers may supply Python objects that cannot come from JSON.
The fleet must isolate these records, retain following good events, and enforce
its exact 64 KiB wire budget before returning the relay response.
"""
from __future__ import annotations

import json
import random

import pytest

from avrone import fleet as fm


class BadFloat(int):
    def __float__(self):
        raise RuntimeError("poisoned numeric conversion")


class EvilEquality:
    def __eq__(self, other):
        raise RuntimeError("poisoned state comparison")


@pytest.mark.parametrize("invalid", [
    float("nan"), float("inf"), float("-inf"), chr(0xD800),
    {"bad": float("nan")}, 10 ** 10000,
], ids=["nan", "inf", "negative-inf", "lone-surrogate", "nested-nan", "huge-int"])
def test_relay_invalid_record_cannot_sink_following_valid_post(invalid):
    out = fm.cap_bridge_posts([
        {"id": "bad", "payload": invalid},
        {"id": "good", "payload": 3},
    ])
    assert out == [{"id": "good", "payload": 3}]
    json.dumps(out, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_relay_rejects_cycles_and_extreme_depth():
    cycle = {}
    cycle["self"] = cycle
    deep = {}
    p = deep
    for _ in range(80):
        p["next"] = {}
        p = p["next"]
    assert fm.cap_bridge_posts([cycle, deep, {"id": "safe"}]) == [{"id": "safe"}]


def test_relay_preserves_shared_noncyclic_containers():
    shared = {"value": 42}
    assert fm.cap_bridge_posts([{"a": shared, "b": shared}]) == [
        {"a": shared, "b": shared}
    ]


def test_relay_skips_oversized_nested_string_before_encoding():
    assert fm.cap_bridge_posts([
        {"payload": {"oversized": "x" * 1000000}}, {"id": "last"}
    ]) == [{"id": "last"}]


def test_relay_retains_newest_with_exact_aggregate_json_budget():
    rows = [{"id": i, "text": "🎯" * 1000} for i in range(50)]
    out = fm.cap_bridge_posts(rows)
    assert 0 < len(out) < len(rows)
    assert out[-1]["id"] == 49
    assert len(json.dumps(out).encode("utf-8")) <= fm.BRIDGE_MAX_BYTES


def test_relay_never_eagerly_copies_entire_collection():
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("unbounded original iteration")
    rows = NoIteration([{"id": i} for i in range(2000)])
    out = fm.cap_bridge_posts(rows)
    assert len(out) == fm.BRIDGE_MAX_POSTS
    assert out[0]["id"] == 1950


@pytest.mark.parametrize("invalid", [
    float("nan"), float("inf"), float("-inf"), 10 ** 10000, BadFloat(5),
], ids=["nan", "inf", "negative-inf", "huge-int", "hostile-float"])
def test_health_rejects_invalid_numeric_without_crashing(invalid):
    assert fm.sanitize_health({"load": invalid, "state": "ok"}) == {"state": "ok"}


def test_health_does_not_call_hostile_state_equality():
    assert fm.sanitize_health({"state": EvilEquality(), "load": 4.5}) == {"load": 4.5}


def test_health_clamps_good_fields_despite_bad_fields():
    assert fm.sanitize_health({
        "load": 10 ** 10000, "mem_pct": 101, "headroom": -2,
        "state": "hot", "active_agents": 10 ** 1000,
    }) == {
        "mem_pct": 100.0, "headroom": 0.0,
        "state": "hot", "active_agents": 10000,
    }


def test_seeded_relay_randomized_valid_json_never_exceeds_limit():
    rng = random.Random(20261115)
    for i in range(300):
        batch = []
        for j in range(rng.randrange(0, 85)):
            kind = rng.randrange(10)
            value = (rng.randrange(-100, 100) if kind < 2 else
                     (float("nan") if kind == 2 else
                     (chr(0xD800) if kind == 3 else
                     ("🎯" * rng.randrange(0, 15000) if kind == 4 else
                     ("x" * rng.randrange(0, 130000) if kind == 5 else
                     {"x": rng.randrange(999)})))))
            batch.append({"id": f"{i}-{j}", "value": value})
        out = fm.cap_bridge_posts(batch)
        assert len(out) <= fm.BRIDGE_MAX_POSTS
        assert len(json.dumps(out, allow_nan=False).encode("utf-8")) <= fm.BRIDGE_MAX_BYTES


def test_relay_exact_byte_boundary_including_brackets():
    empty = {"payload": ""}
    overhead = len(json.dumps(empty))
    exact = {"payload": "x" * (fm.BRIDGE_MAX_BYTES - overhead - 2)}
    over = {"payload": "x" * (fm.BRIDGE_MAX_BYTES - overhead - 1)}
    assert fm._relay_post_size(exact) == fm.BRIDGE_MAX_BYTES - 2
    assert fm.cap_bridge_posts([exact]) == [exact]
    assert fm.cap_bridge_posts([over, {"id": "after"}]) == [{"id": "after"}]


def test_relay_rejects_oversized_node_count():
    assert fm.cap_bridge_posts([
        {"payload": list(range(9000))}, {"id": "fallback"}
    ]) == [{"id": "fallback"}]
