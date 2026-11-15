"""Historical Bridge journals may omit agent/role metadata.

Filtering persisted records must not crash while inspecting older, valid
journal entries that predate the current metadata schema.
"""
from __future__ import annotations

import json

import pytest

from avrone.bridge import Bridge


def _legacy(root, records):
    bridge = Bridge("legacy-agent", root)
    with bridge.path.open("a", encoding="utf-8") as handle:
        for i, record in enumerate(records):
            row = {"id": f"legacy-{i}", "run_id": "legacy-agent",
                   "kind": "finding", "text": f"history-{i}",
                   "ts": i + 1, "refs": []}
            row.update(record)
            handle.write(json.dumps(row) + "\n")
    return Bridge("legacy-agent", root)


@pytest.mark.parametrize("row", [
    {},
    {"role": "engineer"},
    {"agent": "worker"},
    {"agent": None, "role": None},
    {"agent": "different", "role": None},
])
def test_read_agent_filter_does_not_crash_when_metadata_is_missing(tmp_path, row):
    bridge = _legacy(tmp_path, [row])
    assert bridge.read(agent="not-present") == []
    assert len(bridge.posts()) == 1


def test_read_still_matches_role_when_agent_is_missing(tmp_path):
    bridge = _legacy(tmp_path, [{"role": "reviewer"}])
    assert [p["id"] for p in bridge.read(agent="reviewer")] == ["legacy-0"]


def test_read_still_matches_agent_when_role_is_missing(tmp_path):
    bridge = _legacy(tmp_path, [{"agent": "writer"}])
    assert [p["id"] for p in bridge.read(agent="WRITER")] == ["legacy-0"]


def test_mixed_historical_and_current_journal_filtering_is_stable(tmp_path):
    bridge = _legacy(tmp_path, [
        {},
        {"agent": "writer"},
        {"role": "writer"},
        {"agent": "other", "role": "other"},
    ])
    expected = ["legacy-1", "legacy-2"]
    for _ in range(3):
        assert [p["id"] for p in bridge.read(agent="writer")] == expected
        assert [p["id"] for p in Bridge("legacy-agent", tmp_path).read(agent="writer")] == expected


def test_historical_record_without_agent_is_still_queryable_by_text(tmp_path):
    bridge = _legacy(tmp_path, [{}])
    assert [p["id"] for p in bridge.read(query="history-0")] == ["legacy-0"]
