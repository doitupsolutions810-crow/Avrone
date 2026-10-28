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


def test_json_inspect_validates_and_summarizes_without_echoing_values():
    out = run(tx.t_json_inspect(None, {"text": '{"name":"secret-value","items":[1,true,null,{"ok":false}]}'}))
    assert out.startswith("valid JSON: root=object")
    assert "top-level keys (2): 'name', 'items'" in out
    assert "object=2" in out and "array=1" in out
    assert "string=1" in out and "number=1" in out and "boolean=2" in out and "null=1" in out
    assert "secret-value" not in out


def test_json_inspect_rejects_bad_nonstandard_and_oversized_input():
    assert "line 2" in run(tx.t_json_inspect(None, {"text": '{\n "x": }'}))
    assert "non-standard constant NaN" in run(tx.t_json_inspect(None, {"text": "NaN"}))
    assert "must be a JSON string" in run(tx.t_json_inspect(None, {"text": {"x": 1}}))
    assert "exceeds" in run(tx.t_json_inspect(None, {"text": " " * (tx.TEXT_TOOL_MAX_INPUT + 1)}))


def test_json_inspect_registered_as_builtin():
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    assert reg.get("json_inspect") is not None and reg.get("json_inspect").source == "builtin"


def test_text_diff_truncates_large_output():
    out = run(tx.t_text_diff(None, {"old": "", "new": "x" * (tx.TEXT_TOOL_MAX_OUTPUT + 500)}))
    assert out.endswith("... diff truncated ...")
    assert len(out) <= tx.TEXT_TOOL_MAX_OUTPUT + len("\n... diff truncated ...")
