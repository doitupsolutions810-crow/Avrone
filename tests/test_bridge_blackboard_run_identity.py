"""Blackboard run IDs must stay isolated across length and Unicode boundaries."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _post(post_id, run_id):
    return {"id": post_id, "run_id": run_id, "kind": "finding",
            "text": f"payload-{post_id}", "agent": "agent", "ts": 10.0}


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=root))


def test_long_identifiers_with_identical_first_80_chars_are_distinct(tmp_path):
    prefix = "P" * 80
    a, b = prefix + "different-A", prefix + "different-B"
    assert a[:80] == b[:80]
    core = _core(tmp_path)
    duplicate_a, _ = bm.blackboard_accept(core, _post("long-id-a", a))
    duplicate_b, _ = bm.blackboard_accept(core, _post("long-id-b", b))
    assert not duplicate_a and not duplicate_b
    stored = {p["id"]: p for p in bm.blackboard_posts(_core(tmp_path))}
    assert stored["long-id-a"]["run_id"] != stored["long-id-b"]["run_id"]
    assert len(stored["long-id-a"]["run_id"]) <= 80
    assert len(stored["long-id-b"]["run_id"]) <= 80
    assert bm.for_core(core, stored["long-id-a"]["run_id"]).path != bm.for_core(
        core, stored["long-id-b"]["run_id"]).path


@pytest.mark.parametrize("length", [79, 80, 81, 1024, 1025, 10000])
def test_run_id_boundary_length_is_durable_and_within_limit(tmp_path, length):
    raw = "R" * length
    expected = bm._blackboard_run_id(raw)
    assert len(expected) <= 80
    assert bm.seal_blackboard_post(_post("static-id", raw))["run_id"] == expected
    accepted, _ = bm.blackboard_accept(_core(tmp_path), _post("static-id", raw))
    assert not accepted
    reloaded = bm.blackboard_posts(_core(tmp_path))
    assert len(reloaded) == 1
    assert reloaded[0]["run_id"] == expected


def test_nul_and_unicode_normalization_do_not_alias_literal_names():
    assert bm._blackboard_run_id("a\x00b") != bm._blackboard_run_id("ab")
    assert bm._blackboard_run_id("a\ud800") != bm._blackboard_run_id("a\ufffd")
    assert bm._blackboard_run_id("a\udfff") != bm._blackboard_run_id("a\ufffd")


def test_blackboard_wave_alias_remains_backwards_compatible():
    assert bm._blackboard_run_id("first-wave1") == bm._blackboard_run_id("first")
    assert bm._blackboard_run_id("a" * 90 + "-wave1") == bm._blackboard_run_id("a" * 90)


@pytest.mark.parametrize("raw", ["", "   ", "\x00", None])
def test_blank_run_names_still_default_to_run(raw):
    assert bm._blackboard_run_id(raw) == "run"


def test_public_normalization_does_not_pretruncate_run_id(tmp_path):
    first = "X" * 80 + "first"
    second = "X" * 80 + "second"
    a, error_a = bm.normalize_blackboard_post(_post("new-a", first))
    b, error_b = bm.normalize_blackboard_post(_post("new-b", second))
    assert error_a is None and error_b is None
    assert a["run_id"] != b["run_id"]
    core = _core(tmp_path)
    assert bm.blackboard_accept(core, a)[0] is False
    assert bm.blackboard_accept(core, b)[0] is False
    assert {p["id"] for p in bm.blackboard_posts(_core(tmp_path))} == {"new-a", "new-b"}
