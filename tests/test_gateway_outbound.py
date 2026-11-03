"""Round 22: the outbound request body. Lone surrogates, NUL bytes, None/structured content,
odd roles, tool-role messages without tool_call_id, and NaN/out-of-range temperatures used to
crash locally or get a 400 from strict OpenAI-compatible servers. httpx.MockTransport only."""
import asyncio
import json

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.gateway import clean_text, safe_temperature, sanitize_messages


def mk(monkeypatch, handler):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", "2")
    monkeypatch.setenv("AVRONE_LLM_BACKOFF", "0")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(Settings())


def teardown_function():
    gateway.HTTP_TRANSPORT = None


def capture(monkeypatch, messages, temperature=0.7):
    seen = []
    def h(r):
        raw = r.content.decode("utf-8")  # must be valid UTF-8
        seen.append(json.loads(raw))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    g = mk(monkeypatch, h)
    assert asyncio.run(g.chat(messages, temperature=temperature)) == "ok"
    return seen[0]


# --- text cleaning ----------------------------------------------------------------------------

def test_lone_surrogate_no_longer_crashes_the_request(monkeypatch):
    bad = b"\xff\xfebinary".decode("utf-8", "surrogateescape")
    body = capture(monkeypatch, [{"role": "system", "content": "s"}, {"role": "user", "content": bad}])
    assert body["messages"][1]["content"] == "\ufffd\ufffdbinary"


def test_cut_emoji_half_pair():
    assert clean_text("x\ud83dy") == "x\ufffdy"


def test_nul_bytes_dropped():
    assert clean_text("a\x00b\x00") == "ab"


@pytest.mark.parametrize("s", ["plain", "tab\tnl\nCR\r", "emoji 😀", "中文", ""])
def test_clean_text_leaves_good_text_alone(s):
    assert clean_text(s) == s


# --- message shapes ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,want", [
    (None, ""), (42, "42"), ([{"type": "text", "text": "parts"}], "parts"),
    ({"k": "v"}, '{"k": "v"}'), (["a", "b"], "ab"), (b"by", "by"), (True, "True"),
])
def test_content_always_a_string(content, want):
    assert sanitize_messages([{"role": "user", "content": content}]) == [{"role": "user", "content": want}]


@pytest.mark.parametrize("role,want", [
    ("developer", "system"), ("Human", "user"), ("AI", "assistant"), ("model", "assistant"),
    ("  USER ", "user"), (None, "user"), ("", "user"), ("narrator", "user"), (7, "user"),
])
def test_roles_normalised(role, want):
    assert sanitize_messages([{"role": role, "content": "x"}])[0]["role"] == want


def test_tool_role_without_id_becomes_user_tool_result():
    out = sanitize_messages([{"role": "tool", "content": "ls output"}])
    assert out == [{"role": "user", "content": "TOOL RESULT:\nls output"}]


def test_tool_role_already_prefixed_not_double_prefixed():
    out = sanitize_messages([{"role": "function", "content": "TOOL RESULT shell: ok"}])
    assert out == [{"role": "user", "content": "TOOL RESULT shell: ok"}]


def test_tool_role_with_id_kept():
    out = sanitize_messages([{"role": "tool", "tool_call_id": "c1", "content": "ok"}])
    assert out == [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]


def test_extra_keys_dropped_valid_name_kept():
    out = sanitize_messages([{"role": "user", "content": "x", "ts": 123, "name": "lane_2"},
                             {"role": "user", "content": "y", "name": "bad name!"}])
    assert out == [{"role": "user", "content": "x", "name": "lane_2"}, {"role": "user", "content": "y"}]


def test_junk_entries_skipped_and_strings_wrapped():
    out = sanitize_messages([None, 5, "bare string", {"role": "user", "content": "ok"}])
    assert out == [{"role": "user", "content": "bare string"}, {"role": "user", "content": "ok"}]


@pytest.mark.parametrize("msgs,want", [
    (None, []), ({"role": "user", "content": "one"}, [{"role": "user", "content": "one"}]),
    ("hi", [{"role": "user", "content": "hi"}]),
])
def test_container_shapes(msgs, want):
    assert sanitize_messages(msgs) == want


def test_caller_list_not_mutated():
    msgs = [{"role": "developer", "content": None, "ts": 1}]
    sanitize_messages(msgs)
    assert msgs == [{"role": "developer", "content": None, "ts": 1}]


# --- temperature ------------------------------------------------------------------------------

@pytest.mark.parametrize("t,want", [(0.2, 0.2), (float("nan"), 0.7), (float("inf"), 0.7), (-1, 0.0),
                                    (5, 2.0), ("0.4", 0.4), ("hot", 0.7), (None, 0.7)])
def test_safe_temperature(t, want):
    assert safe_temperature(t) == want


def test_nan_temperature_no_longer_crashes(monkeypatch):
    body = capture(monkeypatch, [{"role": "user", "content": "x"}], temperature=float("nan"))
    assert body["temperature"] == 0.7


# --- legacy client path -------------------------------------------------------------------------

def test_legacy_llm_body_is_sanitized_too():
    import inspect
    from avrone import llm
    src = inspect.getsource(llm.LLM._legacy_chat)
    assert "_sanitize(messages)" in src and "_safe_temp(temperature)" in src
