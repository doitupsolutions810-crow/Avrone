"""Round 4 trinity edges: compact_messages must not crash on junk history shapes."""
from __future__ import annotations

import pytest

from avrone.trinity import compact_messages, out_of_turns_result, reply_text


def _long_tool(n: int = 50_000) -> dict:
    return {"role": "user", "content": "TOOL RESULT shell\n" + ("z" * n)}


def test_compact_messages_non_str_content_does_not_raise():
    """A tool result whose content is an int/list used to TypeError in size()."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "user", "content": 12345},
        {"role": "user", "content": ["TOOL RESULT", "x" * 100]},
        _long_tool(),
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "tail-a"},
        {"role": "assistant", "content": "done-a"},
        {"role": "user", "content": "tail-b"},
        {"role": "assistant", "content": "done-b"},
    ]
    # CONTEXT_KEEP_TAIL=4, so index 4 (the long tool result) is eligible to elide.
    n = compact_messages(msgs, 100)
    assert n >= 1
    assert msgs[4]["content"].startswith("TOOL RESULT")
    assert "elided" in msgs[4]["content"].lower() or "[" in msgs[4]["content"]
    assert msgs[0]["content"] == "sys"  # system + first user + tail kept
    assert msgs[1]["content"] == "task"


def test_compact_messages_skips_non_dict_items():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        "not a message",
        None,
        42,
        _long_tool(),
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "tail1"},
        {"role": "assistant", "content": "done1"},
        {"role": "user", "content": "tail2"},
        {"role": "assistant", "content": "done2"},
    ]
    n = compact_messages(msgs, 80)
    assert n >= 1
    assert isinstance(msgs[5]["content"], str)
    assert "elided" in msgs[5]["content"].lower() or "[" in msgs[5]["content"]


def test_compact_messages_rejects_junk_limit_and_non_list():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert compact_messages(msgs, "nope") == 0
    assert compact_messages(msgs, float("nan")) == 0
    assert compact_messages(msgs, float("inf")) == 0
    assert compact_messages(msgs, -10) == 0
    assert compact_messages(None, 100) == 0  # type: ignore[arg-type]
    assert compact_messages({"role": "user"}, 100) == 0  # type: ignore[arg-type]
    assert compact_messages([], 10) == 0


def test_out_of_turns_and_reply_text_on_junk():
    assert "out of turns" in out_of_turns_result(None, "Worker-1", 6)
    assert "out of turns" in out_of_turns_result(b"just bytes", "W", 3) or reply_text(b"just bytes")
    # A model that answers with a final JSON object is preferred over the fallback.
    assert out_of_turns_result('{"final": "shipped"}', "W", 4) == "shipped"
    # A leftover tool call is not a final answer.
    assert "out of turns" in out_of_turns_result('{"tool": "shell", "args": {"cmd": "ls"}}', "W", 2)
