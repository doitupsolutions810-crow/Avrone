"""Adversarial regression for bridge limits, lesson budgets, and tombstone replay."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


@pytest.mark.parametrize("value", [
    float("inf"), float("-inf"), float("nan"),
    "9" * 5000, [], {}, object(),
], ids=["inf", "minus-inf", "nan", "overlong-int", "list", "dict", "object"])
def test_bridge_read_malformed_limit_falls_back_to_safe_default(tmp_path, value):
    bridge = bm.Bridge("guard-read", tmp_path)
    bridge.post("unit", "finding", "safe-data")
    rows = bridge.read(limit=value)
    assert len(rows) == 1 and rows[0]["text"] == "safe-data"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "9" * 5000])
async def test_bridge_tool_read_malformed_limit_is_safe(tmp_path, value):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    bm.for_core(core, "bounded-tool").post("unit", "finding", "retained-value")
    ctx = SimpleNamespace(core=core, run_id="bounded-tool")
    result = await bm.t_bridge_read(ctx, {"limit": value})
    assert "retained-value" in result


@pytest.mark.parametrize("value", [0, -1, -100])
def test_zero_or_negative_lesson_budget_writes_nothing(tmp_path, value):
    bridge = bm.Bridge("no-lessons", tmp_path)
    bridge.post("unit", "conclusion", "persistent learning should be disabled")
    assert bm.distill_lessons(tmp_path, bridge, k=value) == []
    assert not (tmp_path / "memory.jsonl").exists()


def test_late_tombstone_removes_existing_record_on_replay(tmp_path):
    bridge = bm.Bridge("tomb-final", tmp_path)
    p1 = bridge.post("unit", "finding", "obsolete")
    p2 = bridge.post("unit", "finding", "retained")
    with bridge.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tombstones": [p1["id"]]}) + "\n")
    reopened = bm.Bridge("tomb-final", tmp_path)
    assert reopened.get(p1["id"]) is None
    assert reopened.get(p2["id"])["text"] == "retained"
    assert reopened.merge_remote([dict(p1)], "peer")["skipped"] == 1
