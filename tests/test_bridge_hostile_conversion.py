"""Hostile in-process metadata cannot crash Bridge conversion helpers."""
from __future__ import annotations

import math
import time

import pytest

from avrone import bridge as bm


class RaiseTypeError:
    def __str__(self):
        raise TypeError("injected string conversion error")


class RaiseRuntimeError:
    def __str__(self):
        raise RuntimeError("injected string conversion error")


class NonStringResult:
    def __str__(self):
        return 77  # Python must reject this, but the Bridge must contain it


class BadFloat(int):
    def __float__(self):
        raise TypeError("injected numeric conversion error")


@pytest.mark.parametrize("value", [
    pytest.param(RaiseTypeError(), id="type-error"),
    pytest.param(RaiseRuntimeError(), id="runtime-error"),
    pytest.param(NonStringResult(), id="nonstr-result"),
])
def test_utf8_helper_contains_broken_str_implementations(value):
    assert bm._utf8_text(value) == "..."
    assert bm._clip(value) == "..."


@pytest.mark.parametrize("where", ["agent", "role", "text", "node_id"])
def test_local_bridge_contains_hostile_metadata(tmp_path, where):
    kwargs = {"node_id": RaiseTypeError()} if where == "node_id" else {}
    bridge = bm.Bridge("hostile-metadata", tmp_path, **kwargs)
    agent = RaiseRuntimeError() if where == "agent" else "worker"
    role = NonStringResult() if where == "role" else "reviewer"
    body = RaiseTypeError() if where == "text" else "normal-body"
    posted = bridge.post(agent, "finding", body, role=role)
    assert posted["agent"] == ("..." if where == "agent" else "worker")
    assert posted["role"] == ("..." if where == "role" else "reviewer")
    assert posted["text"] == ("..." if where == "text" else "normal-body")
    assert posted["origin_node"] == ("..." if where == "node_id" else "local")
    assert bm.Bridge("hostile-metadata", tmp_path).get(posted["id"]) is not None


def test_peer_origin_uses_fallback_on_hostile_stringifier(tmp_path):
    bridge = bm.Bridge("hostile-origin", tmp_path)
    stats = bridge.merge_remote([{"id": "remote-id", "kind": "finding",
                                  "text": "retained", "agent": "peer"}],
                                RaiseRuntimeError())
    assert stats["merged"] == 1
    assert bridge.get("remote-id")["origin_node"] == "..."


def test_public_normalization_contains_hostile_text():
    value, error = bm.normalize_blackboard_post({
        "id": "safe-id", "kind": "finding", "text": NonStringResult()
    })
    assert error is None
    assert value["text"] == "..."


def test_int_subclass_bad_float_conversion_is_bounded():
    start = time.time()
    actual = bm._remote_ts(BadFloat(14))
    assert isinstance(actual, float) and math.isfinite(actual)
    assert start <= actual <= time.time() + 1


def test_normal_positive_numeric_timestamp_remains_unchanged():
    assert bm._remote_ts(1800000000) == 1800000000.0
