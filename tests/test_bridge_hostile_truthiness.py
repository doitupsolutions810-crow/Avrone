"""Hostile Python truthiness must never bypass AVRONE Bridge validation.

In-process tools and plugins can supply objects with overridden __bool__,
__str__, or inherited str behavior. Guard admission and journal identities.
"""
from __future__ import annotations

import json
import random

import pytest

from avrone import bridge as bm


class HostileBool:
    def __init__(self, value="worker"):
        self.value = value

    def __bool__(self):
        raise RuntimeError("untrusted truthiness")

    def __str__(self):
        return self.value


class HostileText(str):
    def __bool__(self):
        raise RuntimeError("subclass truthiness")

    def __str__(self):
        raise RuntimeError("subclass string conversion")


@pytest.mark.parametrize("field,content", [
    ("kind", "finding"), ("agent", "writer"), ("role", "reviewer"),
    ("origin_node", "node-1"), ("run_id", "tenant-A"),
])
@pytest.mark.parametrize("factory", [HostileBool, HostileText])
def test_blackboard_normalization_never_invokes_hostile_bool(field, content, factory):
    post, error = bm.normalize_blackboard_post({
        "id": "safe-id", "kind": "finding", "text": "hello", field: factory(content)
    })
    assert error is None
    assert post[field] == content
    json.dumps(post, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize("field,content", [
    ("agent", "writer"), ("role", "reviewer"),
    ("origin_node", "node-1"), ("run_id", "tenant-A"),
])
def test_seal_does_not_test_untrusted_truthiness(field, content):
    sealed = bm.seal_blackboard_post({
        "id": "sealed", "kind": "finding", "text": "hello", field: HostileBool(content)
    })
    assert sealed[field] == content


@pytest.mark.parametrize("field,content", [
    ("agent", "worker"), ("role", "reviewer"), ("kind", "finding")
])
@pytest.mark.parametrize("factory", [HostileBool, HostileText])
def test_local_post_handles_hostile_truthiness(tmp_path, field, content, factory):
    b = bm.Bridge("truth-local", tmp_path)
    kwargs = {"agent": "worker", "kind": "finding", "text": "accepted", "role": "reviewer"}
    kwargs[field] = factory(content)
    post = b.post(**kwargs)
    assert post[field] == content
    assert bm.Bridge("truth-local", tmp_path).get(post["id"]) is not None


def test_hostile_node_and_run_identifiers_remain_durable(tmp_path):
    b = bm.Bridge(HostileText("tenant-A"), tmp_path,
                  node_id=HostileBool("node-1"))
    post = b.post("writer", "finding", "accepted")
    assert post["run_id"] == "tenant-A"
    assert post["origin_node"] == "node-1"
    assert bm.Bridge("tenant-A", tmp_path).get(post["id"]) is not None


def test_hostile_origin_does_not_abort_remote_batch(tmp_path):
    bridge = bm.Bridge("truth-remote", tmp_path)
    stats = bridge.merge_remote([
        {"id": "peer-event", "kind": "finding", "text": "accepted"}
    ], HostileBool("peer-1"))
    assert stats["merged"] == 1
    assert bridge.get("peer-event")["origin_node"] == "peer-1"


def test_run_identity_normalization_preserves_collision_resistance():
    assert bm.bridge_run_id("tenant\ud800") != bm.bridge_run_id("tenant\ufffd")
    assert bm._blackboard_run_id("tenant\ud800") != bm._blackboard_run_id("tenant\ufffd")
    assert bm.bridge_run_id(HostileText("x-wave1")) == "x"
    assert bm.safe_run_id(HostileText("a/b")) == bm.safe_run_id("a/b")


def test_zero_and_missing_run_id_defaults_are_backward_compatible():
    assert bm.bridge_run_id(0) == "run"
    assert bm.safe_run_id(0) == bm.safe_run_id(None)
    for run_id in (0, False, None, ""):
        assert bm._blackboard_run_id(run_id) == "run"


def test_300_randomized_hostile_blackboard_values_preserve_utf8():
    rng = random.Random(2026111502)
    for n in range(300):
        factory = rng.choice([HostileBool, HostileText])
        post, error = bm.normalize_blackboard_post({
            "id": f"evt-{n}", "kind": factory("finding"),
            "text": "accepted", "agent": factory("worker"),
            "role": factory("reviewer"), "origin_node": factory("node"),
            "run_id": factory("tenant"),
        })
        assert error is None
        assert post["id"] == f"evt-{n}"
        assert post["run_id"] == "tenant"
        assert post["agent"] == "worker"
        json.dumps(post, ensure_ascii=False, allow_nan=False).encode("utf-8")
