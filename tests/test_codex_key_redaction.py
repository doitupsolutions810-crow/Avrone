"""Persistent and live Codex event data must mask secrets in keys and values."""
from __future__ import annotations

import json

import pytest

from avrone.codex import Codex


@pytest.mark.parametrize("shape", ["root-key", "nested-key", "nested-list-key"])
def test_sensitive_mapping_keys_never_leak_to_audit_or_subscribers(tmp_path, monkeypatch, shape):
    secret = "edge-secret-AVRONE-947123987"
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", secret)
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "on")
    codex = Codex(tmp_path)
    queue = codex.subscribe()
    if shape == "root-key":
        payload = {f"unsafe_{secret}": "ordinary-value"}
    elif shape == "nested-key":
        payload = {"meta": {f"unsafe_{secret}": "ordinary-value"}}
    else:
        payload = {"meta": [{f"unsafe_{secret}": "ordinary-value"}]}
    emitted = codex.emit("key-redact", "tool.call", "agent", **payload)
    delivered = queue.get_nowait()
    assert delivered is emitted
    for exported in (
        json.dumps(emitted.data),
        json.dumps(delivered.data),
        json.dumps(codex.replay("key-redact")),
        (codex.log_dir / "key-redact.jsonl").read_text(),
    ):
        assert secret not in exported


def test_non_sensitive_audit_keys_remain_unchanged(tmp_path):
    codex = Codex(tmp_path)
    original = {"post_kind": "finding", "refs": ["abc"], "nested": {"ordinary": 42}}
    emitted = codex.emit("normal-keys", "bridge.post", "agent", **original)
    assert emitted.data == original
    assert codex.replay("normal-keys")[0]["data"] == original

