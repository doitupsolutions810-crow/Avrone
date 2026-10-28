"""Offline tests for the deterministic text_diff agent tool."""
import asyncio

from avrone import tools_extra as tx
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_text_diff_changes_labels_and_context():
    out = run(tx.t_text_diff(None, {
        "old": "alpha\nbeta\n",
        "new": "alpha\nBETA\ngamma\n",
        "fromfile": "old.txt",
        "tofile": "new.txt",
        "context": 0,
    }))
    assert "--- old.txt" in out and "+++ new.txt" in out
    assert "-beta" in out and "+BETA" in out and "+gamma" in out


def test_text_diff_noop_input_validation_and_bounds():
    assert run(tx.t_text_diff(None, {"old": "same", "new": "same"})) == "no differences"
    assert run(tx.t_text_diff(None, {"old": 5, "new": "x"})).startswith("ERROR:")
    assert "exceeds" in run(tx.t_text_diff(None, {
        "old": "x" * tx.TEXT_TOOL_MAX_INPUT,
        "new": "y",
    }))
    out = run(tx.t_text_diff(None, {"old": "a\n", "new": "b\n", "fromfile": "bad\nname"}))
    assert "bad_name" in out and "bad\nname" not in out


def test_text_diff_registered_as_builtin():
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    assert reg.get("text_diff") is not None
    assert reg.get("text_diff").source == "builtin"
