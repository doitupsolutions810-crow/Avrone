"""Reject IDs that Python's regex $ anchor accepts before a trailing newline.

All external journal, relay, cursor and blackboard IDs must match the entire
string, never only a prefix. A terminal newline is a real character even
though re.Pattern.match with '$' accepts the preceding prefix.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


@pytest.mark.parametrize("invalid", [
    "post-id\n",
    ("z" * 80) + "\n",
    "post-id\n\n",
    "post-id\r",
    "post-id\x00",
])
def test_remote_merge_refuses_invalid_terminal_characters(tmp_path, invalid):
    bridge = bm.Bridge("invalid-id-remote", tmp_path)
    item = {"id": invalid, "kind": "finding", "text": "forged", "refs": []}
    stats = bridge.merge_remote([item], "remote")
    assert stats == {"merged": 0, "skipped": 0, "rejected": 1}
    assert bridge.posts() == []


@pytest.mark.parametrize("invalid", ["post-id\n", ("x" * 80) + "\n"])
def test_persisted_journal_post_rejects_newline_id(tmp_path, invalid):
    bridge = bm.Bridge("invalid-id-journal", tmp_path)
    original = bridge.post("trusted", "finding", "preserve")
    with bridge.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": invalid, "run_id": bridge.run_id, "kind": "finding",
            "text": "untrusted", "agent": "other", "refs": [], "ts": 1
        }) + "\n")
    restored = bm.Bridge("invalid-id-journal", tmp_path)
    assert [p["id"] for p in restored.posts()] == [original["id"]]


def test_tombstone_replay_ignores_newline_id_and_keeps_valid_id():
    line = json.dumps({"tombstones": ["bad\n", "safe", "safe\n"]})
    assert bm.Bridge._parse_tombstones(line) == ["safe"]


@pytest.mark.parametrize("invalid", ["newline\n", "a" * 80 + "\n"])
def test_blackboard_normalization_rejects_invalid_id(invalid):
    obj, error = bm.normalize_blackboard_post({
        "id": invalid, "run_id": "test", "kind": "finding", "text": "hello"
    })
    assert obj is None
    assert error == "bad post id"


def test_blackboard_accept_rejects_newline_id_before_storage(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(bm.BridgeError, match="bad post id"):
        bm.blackboard_accept(core, {
            "id": "forged\n", "run_id": "test", "kind": "finding", "text": "hello"
        })
    assert not (tmp_path / "bridge" / "test.jsonl").exists()


def test_persisted_cursor_registry_ignores_newline_id(tmp_path):
    folder = tmp_path / "bridge"
    folder.mkdir()
    (folder / "_fanin_order.json").write_text(
        json.dumps({"ids": ["unsafe\n", "valid", "valid\n"]}),
        encoding="utf-8",
    )
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    assert list(bm._load_order(core)) == ["valid"]


def test_normal_ids_and_colon_dash_underscore_remain_accepted(tmp_path):
    bridge = bm.Bridge("strict-good", tmp_path)
    item = {"id": "valid-id:version_1.2", "kind": "finding",
            "text": "retained", "refs": []}
    assert bridge.merge_remote([item], "remote")["merged"] == 1
    assert bm.Bridge("strict-good", tmp_path).get(item["id"])["text"] == "retained"
