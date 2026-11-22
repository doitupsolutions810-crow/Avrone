"""Bridge retention boundaries must be safe for malformed operator inputs."""
import math

import pytest

from avrone.bridge import Bridge, DEFAULT_MAX_POSTS, HARD_MAX_POSTS


@pytest.mark.parametrize(
    "value, expected",
    [
        (-100, 1), (-1, 1), ("-9", 1), (1, 1), (3, 3),
        (0, DEFAULT_MAX_POSTS), (None, DEFAULT_MAX_POSTS),
        ("not-an-integer", DEFAULT_MAX_POSTS),
        (float("nan"), DEFAULT_MAX_POSTS),
        (float("inf"), DEFAULT_MAX_POSTS),
        (10 ** 1000, HARD_MAX_POSTS),
    ],
    ids=["negative", "minus-one", "negative-string", "one", "three",
         "zero-default", "none-default", "invalid-string", "nan", "infinity",
         "huge-integer"],
)
def test_explicit_bridge_limit_is_bounded(tmp_path, monkeypatch, value, expected):
    monkeypatch.delenv("AVRONE_BRIDGE_MAX_POSTS", raising=False)
    bridge = Bridge("bounded", tmp_path, limit=value)
    assert bridge.limit == expected


def test_negative_bridge_limit_retains_last_post_without_raising(tmp_path):
    bridge = Bridge("negative", tmp_path, limit=-400)
    writes = [bridge.post("worker", "finding", f"finding {n}") for n in range(30)]
    assert [p["id"] for p in bridge.posts()] == [writes[-1]["id"]]

    reopened = Bridge("negative", tmp_path, limit=-400)
    assert [p["id"] for p in reopened.posts()] == [writes[-1]["id"]]


def test_oversized_limit_does_not_disable_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "3")
    bridge = Bridge("large", tmp_path, limit=10 ** 1000)
    assert bridge.limit == HARD_MAX_POSTS
    for idx in range(6):
        bridge.post("worker", "finding", str(idx))
    assert len(bridge.posts()) == 6
    assert len(Bridge("large", tmp_path, limit=10 ** 1000).posts()) == 6
