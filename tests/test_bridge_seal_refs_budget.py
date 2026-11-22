"""Reference validation must be bounded *before* sanitizing the inputs."""
from __future__ import annotations

from collections import UserDict

import pytest

from avrone import bridge as bm


def _post(refs):
    return {"id": "ref-budget-post", "run_id": "budget-run",
            "kind": "finding", "agent": "writer", "text": "hello",
            "refs": refs}


def test_hundred_thousand_invalid_references_are_not_all_converted(monkeypatch):
    original = bm._utf8_text
    calls = []
    def counted(value):
        calls.append(value)
        if len(calls) > bm.MAX_REF_SCAN + 16:
            raise AssertionError("unbounded pre-sanitization")
        return original(value)

    monkeypatch.setattr(bm, "_utf8_text", counted)
    sealed = bm.seal_blackboard_post(_post(["invalid id with spaces"] * 100_000))
    assert sealed["refs"] == []
    assert len(calls) <= bm.MAX_REF_SCAN + 16


def test_late_reference_beyond_scan_limit_is_not_observed():
    class Explosive:
        def __str__(self):
            raise AssertionError("late reference was inspected")
    refs = ["bad reference"] * bm.MAX_REF_SCAN + [Explosive()]
    assert bm.seal_blackboard_post(_post(refs))["refs"] == []


def test_list_subclass_must_not_be_iterated_before_bounded_slice():
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("unbounded list iteration")
    refs = NoIteration(["bad reference"] * (bm.MAX_REF_SCAN + 100))
    assert bm.seal_blackboard_post(_post(refs))["refs"] == []


@pytest.mark.parametrize("refs,expected", [
    (["valid", "valid", "other"], ["valid", "other"]),
    (("valid", "other"), ["valid", "other"]),
    ("valid", ["valid"]),
    ({"valid": "other"}, []),
    (UserDict({"valid": "other"}), []),
    (None, []),
])
def test_known_reference_shapes_are_normalized_without_expansion(refs, expected):
    assert bm.seal_blackboard_post(_post(refs))["refs"] == expected


def test_only_first_256_candidates_can_be_accepted():
    refs = ["invalid id"] * bm.MAX_REF_SCAN + ["valid-late"]
    assert bm.seal_blackboard_post(_post(refs))["refs"] == []


def test_direct_blackboard_accept_large_refs_does_not_persist_late_reference(tmp_path):
    from types import SimpleNamespace
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    refs = ["invalid id"] * (bm.MAX_REF_SCAN + 500) + ["late"]
    duplicate, post_id = bm.blackboard_accept(core, _post(refs))
    assert duplicate is False and post_id == "ref-budget-post"
    restored = bm.blackboard_posts(SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path)))
    assert len(restored) == 1
    assert restored[0]["refs"] == []
