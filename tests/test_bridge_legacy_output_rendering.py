"""Legacy Bridge output should not crash on absent/null/empty agent metadata."""
from __future__ import annotations

import json

import pytest

from avrone.bridge import Bridge, compact


def _legacy(tmp_path, kind, agent_value="MISSING", role_value="MISSING"):
    bridge = Bridge("legacy-output", tmp_path)
    post = {"id": "legacy-entry", "run_id": "legacy-output", "kind": kind,
            "text": "legacy text", "refs": [], "ts": 7}
    if agent_value != "MISSING":
        post["agent"] = agent_value
    if role_value != "MISSING":
        post["role"] = role_value
    with bridge.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(post) + "\n")
    return Bridge("legacy-output", tmp_path)


@pytest.mark.parametrize("kind", [
    "finding", "question", "answer", "correction", "conclusion"
])
@pytest.mark.parametrize("agent_value", ["MISSING", None, ""])
def test_compact_handles_legacy_agent_shapes(tmp_path, kind, agent_value):
    bridge = _legacy(tmp_path, kind, agent_value)
    output = compact(bridge.posts())
    assert "[legacy-entry]" in output
    assert f"{kind} agent:" in output
    assert "legacy text" in output


@pytest.mark.parametrize("kind", ["question", "correction", "conclusion"])
@pytest.mark.parametrize("agent_value", ["MISSING", None, ""])
def test_digest_handles_legacy_agent_shapes(tmp_path, kind, agent_value):
    bridge = _legacy(tmp_path, kind, agent_value)
    output = bridge.digest()
    assert "[legacy-entry] agent: legacy text" in output


def test_explicit_agent_and_role_are_not_replaced(tmp_path):
    bridge = _legacy(tmp_path, "conclusion", "reviewer", "approver")
    assert "[legacy-entry] conclusion reviewer:" in compact(bridge.posts())
    assert "[legacy-entry] reviewer: legacy text" in bridge.digest()
    assert bridge.read(agent="approver")[0]["id"] == "legacy-entry"


def test_legacy_output_is_stable_after_restart(tmp_path):
    bridge = _legacy(tmp_path, "conclusion", None, "manager")
    first = bridge.digest()
    assert first == Bridge("legacy-output", tmp_path).digest()
    assert compact(bridge.posts()) == compact(Bridge("legacy-output", tmp_path).posts())
