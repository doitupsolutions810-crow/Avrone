"""Global fan-in cursor registry must not be read without a byte cap."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from avrone import bridge as bm


def _core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


def _cursor(tmp_path, raw):
    folder = tmp_path / "bridge"
    folder.mkdir(exist_ok=True)
    path = folder / "_fanin_order.json"
    path.write_bytes(raw)
    return path


def _padded_json(size):
    template = json.dumps({"ids": ["valid"], "pad": ""}, separators=(",", ":"))
    if size < len(template):
        raise AssertionError("test cap too small")
    return json.dumps({"ids": ["valid"], "pad": "x" * (size - len(template))},
                      separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("size", [256, 257, 4096, 65_536])
def test_oversized_order_registry_is_ignored(tmp_path, monkeypatch, size):
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", 255)
    path = _cursor(tmp_path, _padded_json(size))
    assert path.stat().st_size == size
    assert list(bm._load_order(_core(tmp_path))) == []


@pytest.mark.parametrize("size", [64, 128, 255])
def test_order_registry_at_or_under_byte_limit_is_read(tmp_path, monkeypatch, size):
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", 255)
    _cursor(tmp_path, _padded_json(size))
    assert list(bm._load_order(_core(tmp_path))) == ["valid"]


@pytest.mark.parametrize("raw", [
    b"\xff\xfe",
    b'{"ids": [',
    b'{"ids": null}',
    b'["not-a-registry"]',
])
def test_bad_registry_content_fails_to_empty_order(tmp_path, raw):
    _cursor(tmp_path, raw)
    assert list(bm._load_order(_core(tmp_path))) == []


def test_order_registry_deduplicates_ids_without_losing_first_seen(tmp_path):
    _cursor(tmp_path, json.dumps({"ids": ["a", "b", "a", "c", "b"]}).encode("utf-8"))
    assert list(bm._load_order(_core(tmp_path))) == ["a", "b", "c"]


def test_registry_read_is_explicitly_bounded(tmp_path, monkeypatch):
    """The loader must request no more than MAX_ORDER_BYTES+1 bytes."""
    monkeypatch.setattr(bm, "MAX_ORDER_BYTES", 255)
    _cursor(tmp_path, _padded_json(5_000_000))
    original_fdopen = bm.os.fdopen
    observed = []

    class Guard:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            observed.append(size)
            assert 0 <= size <= bm.MAX_ORDER_BYTES + 1
            return self.stream.read(size)

    def guarded_fdopen(fd, mode="r", *args, **kwargs):
        return Guard(original_fdopen(fd, mode, *args, **kwargs))

    monkeypatch.setattr(bm.os, "fdopen", guarded_fdopen)
    assert list(bm._load_order(_core(tmp_path))) == []
    assert observed == [256]
