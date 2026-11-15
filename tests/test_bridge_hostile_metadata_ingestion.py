"""Adversarial in-process values must not crash blackboard ingestion.

The HTTP path admits JSON primitives, but Python tools and agent/plugin code can
call these APIs with user-defined objects. A bad conversion must affect only its
own record, not the rest of a fleet batch or a persisted cursor.
"""
from __future__ import annotations

import json
import math
import random

import pytest

from avrone import bridge as bm


class CrashingString:
    def __str__(self):
        raise RuntimeError("injected __str__ failure")


class BrokenString:
    def __str__(self):
        return 19  # violates Python's __str__ contract


class BadFloat(int):
    def __float__(self):
        raise TypeError("injected float conversion failure")


@pytest.mark.parametrize("field", ["agent", "role", "origin_node"])
@pytest.mark.parametrize("factory", [CrashingString, BrokenString])
def test_normalize_contains_hostile_metadata_conversion(field, factory):
    record, error = bm.normalize_blackboard_post({
        "id": "hostile-meta", "run_id": "tenant-A", "kind": "finding",
        "text": "valid", field: factory(),
    })
    assert error is None
    assert record[field] == "..."
    assert record["text"] == "valid"
    json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_bad_remote_conversion_rejects_one_record_but_accepts_next(tmp_path):
    b = bm.Bridge("remote-str-recovery", tmp_path)
    rows = [
        {"id": "invalid", "kind": "finding", "text": "poison",
         "agent": CrashingString()},
        {"id": "valid", "kind": "finding", "text": "retained", "agent": "peer"},
    ]
    assert b.merge_remote(rows, "peer") == {"merged": 1, "skipped": 0, "rejected": 1}
    assert b.get("invalid") is None
    assert b.get("valid")["text"] == "retained"
    assert [r["id"] for r in bm.Bridge("remote-str-recovery", tmp_path).posts()] == ["valid"]


def test_malformed_local_kind_yields_bridge_error_instead_of_runtime_error(tmp_path):
    b = bm.Bridge("hostile-kind", tmp_path)
    with pytest.raises(bm.BridgeError, match="kind must be one of"):
        b.post("agent", CrashingString(), "not persisted")
    assert b.posts() == []


def test_poisoned_timestamp_comparator_has_finite_fallback():
    assert bm._ts_key({"id": "safe", "ts": BadFloat(7)}) == (0.0, "safe")


def test_randomized_metadata_is_json_serializable_without_agent_crash():
    rng = random.Random(20261115)
    choices = [
        "simple", "embedded\\x00nul", "\\ud800", "é" * 200,
        CrashingString(), BrokenString(), 0, None, True,
    ]
    for i in range(512):
        record, error = bm.normalize_blackboard_post({
            "id": f"fuzz-{i}", "run_id": "fuzz-tenant",
            "kind": "finding", "text": f"valid-{i}",
            "agent": rng.choice(choices),
            "role": rng.choice(choices),
            "origin_node": rng.choice(choices),
        })
        assert error is None
        assert record["id"] == f"fuzz-{i}"
        assert record["text"] == f"valid-{i}"
        assert math.isfinite(record["ts"])
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
