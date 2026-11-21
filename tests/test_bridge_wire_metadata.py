"""UTF-8 and NUL safety for local Bridge metadata and peer-origin fallbacks."""
from __future__ import annotations

import json

import pytest

from avrone.bridge import Bridge


def _assert_wire_safe(record):
    wire = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert b"\x00" not in wire
    assert "\ufffd" in wire.decode("utf-8")
    return wire


@pytest.mark.parametrize("field", ["run_id", "agent", "role", "node_id"])
@pytest.mark.parametrize("bad", ["\ud800", "\udfff", "\ud800\x00"])
def test_local_post_metadata_is_utf8_safe_on_wire_and_restart(tmp_path, field, bad):
    value = "edge-" + bad + "-suffix"
    kwargs = {"node_id": value} if field == "node_id" else {}
    run_id = value if field == "run_id" else "metadata-safe"
    bridge = Bridge(run_id, tmp_path, **kwargs)
    posted = bridge.post(
        value if field == "agent" else "agent",
        "finding",
        "persisted",
        role=value if field == "role" else "role",
    )
    _assert_wire_safe(posted)
    reopened = Bridge(run_id, tmp_path, **kwargs)
    stored = reopened.get(posted["id"])
    assert stored is not None
    _assert_wire_safe(stored)
    assert stored["text"] == "persisted"


def test_different_invalid_unicode_run_ids_do_not_collide(tmp_path):
    malformed = "shared\ud800"
    normalized = "shared\ufffd"
    first = Bridge(malformed, tmp_path)
    second = Bridge(normalized, tmp_path)
    assert first.run_id != second.run_id
    assert first.path != second.path
    a = first.post("a", "finding", "invalid-raw")
    b = second.post("b", "finding", "replacement-literal")
    assert a["id"] != b["id"]
    assert [p["text"] for p in Bridge(malformed, tmp_path).posts()] == ["invalid-raw"]
    assert [p["text"] for p in Bridge(normalized, tmp_path).posts()] == ["replacement-literal"]


def test_removed_nul_in_run_id_does_not_alias_clean_name(tmp_path):
    with_nul = Bridge("same\x00name", tmp_path)
    without_nul = Bridge("samename", tmp_path)
    assert with_nul.run_id != without_nul.run_id
    assert with_nul.path != without_nul.path


def test_wave1_alias_survives_unicode_normalization(tmp_path):
    first = Bridge("wave\ud800", tmp_path)
    alias = Bridge("wave\ud800-wave1", tmp_path)
    assert first.run_id == alias.run_id
    assert first.path == alias.path


@pytest.mark.parametrize("bad", ["\ud800", "\udfff", "\x00", "\ud800\x00"])
def test_relay_origin_fallback_is_wire_safe(tmp_path, bad):
    bridge = Bridge("remote-wire", tmp_path)
    payload = {"id": "relay-001", "kind": "finding", "text": "accepted",
               "agent": "relay", "refs": [], "ts": 1}
    stats = bridge.merge_remote([payload], "peer" + bad + "edge")
    assert stats["merged"] == 1
    record = bridge.get("relay-001")
    assert record is not None
    wire = json.dumps(record, ensure_ascii=False).encode("utf-8")
    assert b"\x00" not in wire
    assert bridge.get("relay-001")["origin_node"] == record["origin_node"]
