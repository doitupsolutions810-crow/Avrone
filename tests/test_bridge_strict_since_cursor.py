"""Blackboard cursors must be exact bounded IDs, not normalized aliases."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from avrone import bridge as bm


class ExplosiveCursor:
    def __str__(self):
        raise AssertionError("non-string cursor must not be converted")


@pytest.mark.parametrize("cursor", [
    None, 123, True, pytest.param(10 ** 6000, id="huge-integer"),
    ExplosiveCursor(), "alpha\x00beta", "alpha\n", "alpha\r",
    "\ud800", "x" * 81, "", "spaces are invalid",
])
def test_malformed_or_non_string_cursor_is_ignored_without_coercion(cursor):
    assert bm.safe_since(cursor) is None


@pytest.mark.parametrize("cursor", ["abc", "ABC_123.-:xyz", "z" * 80])
def test_valid_exact_post_id_cursor_is_preserved(cursor):
    assert bm.safe_since(cursor) == cursor


def _core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


def _accept(core, post_id):
    duplicate, returned = bm.blackboard_accept(core, {
        "id": post_id, "run_id": "cursor-run", "kind": "finding",
        "text": post_id, "agent": "writer", "ts": 1,
    })
    assert duplicate is False and returned == post_id


def test_nul_cursor_does_not_alias_an_existing_id(tmp_path):
    core = _core(tmp_path)
    _accept(core, "alphabeta")
    _accept(core, "following")
    assert [p["id"] for p in bm.blackboard_posts(core, since="alpha\x00beta")] == [
        "alphabeta", "following"
    ]
    assert [p["id"] for p in bm.blackboard_posts(core, since="alphabeta")] == [
        "following"
    ]


def test_numeric_cursor_does_not_alias_a_string_id(tmp_path):
    core = _core(tmp_path)
    _accept(core, "123")
    _accept(core, "next")
    assert [p["id"] for p in bm.blackboard_posts(core, since=123)] == [
        "123", "next"
    ]
    assert [p["id"] for p in bm.blackboard_posts(core, since="123")] == [
        "next"
    ]


def test_very_long_cursor_is_rejected_before_string_processing():
    assert bm.safe_since("a" * 1_000_000) is None
