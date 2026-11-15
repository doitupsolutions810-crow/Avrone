"""Explicit blackboard post IDs must be genuine strings, never coerced objects."""
from __future__ import annotations

from types import SimpleNamespace
import pytest

from avrone import bridge as bm


class ExplosiveString:
    def __str__(self):
        raise AssertionError("should not call __str__ for invalid ID")


@pytest.mark.parametrize("invalid", [
    0, 1, 123, True, False, -1, 1.0, 1.5, float("nan"),
    [], {}, ["text"], {"value": "id"}, 10 ** 5000, ExplosiveString(),
])
def test_public_normalizer_rejects_non_string_explicit_post_ids(invalid):
    obj, error = bm.normalize_blackboard_post({
        "id": invalid, "run_id": "string-ids", "kind": "finding",
        "text": "payload", "agent": "agent",
    })
    assert obj is None
    assert error == "bad post id"


@pytest.mark.parametrize("invalid", [123, True, False, 0, [], {}, ExplosiveString()])
def test_direct_blackboard_admission_rejects_non_string_ids(tmp_path, invalid):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(bm.BridgeError, match="bad post id"):
        bm.blackboard_accept(core, {
            "id": invalid, "run_id": "typed-ids", "kind": "finding",
            "text": "should-not-persist", "agent": "agent",
        })
    assert bm.blackboard_posts(core) == []


def test_explicit_digit_string_remains_valid_and_distinct_from_number():
    valid, error = bm.normalize_blackboard_post({
        "id": "123", "kind": "finding", "text": "retained"
    })
    assert error is None
    assert valid["id"] == "123"
    rejected, error = bm.normalize_blackboard_post({
        "id": 123, "kind": "finding", "text": "rejected"
    })
    assert rejected is None and error == "bad post id"


@pytest.mark.parametrize("data", [
    {"kind": "finding", "text": "no-id"},
    {"id": None, "kind": "finding", "text": "null-id"},
    {"id": "", "kind": "finding", "text": "empty-id"},
])
def test_missing_or_empty_id_still_generates_valid_string(data):
    post, error = bm.normalize_blackboard_post(data)
    assert error is None
    assert isinstance(post["id"], str)
    assert bm._ID_RE.fullmatch(post["id"])
