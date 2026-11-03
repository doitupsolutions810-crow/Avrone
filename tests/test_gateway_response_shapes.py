"""Round 21: 200 bodies that are not the plain OpenAI chat shape: {"error": ...} with status 200,
legacy completions, a lone streamed chunk, Ollama's native /api/chat body, an SSE stream from a
proxy that forces streaming, plus Retry-After as an HTTP-date. httpx.MockTransport only."""
import asyncio
import json
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from avrone import gateway
from avrone.config import Settings
from avrone.gateway import LLMError, extract_choice, retry_after_seconds, _delay
from avrone.trinity import parse_action

MSG = [{"role": "system", "content": "agent"}, {"role": "user", "content": "hi"}]


def mk(monkeypatch, handler, retries="3"):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", retries)
    monkeypatch.setenv("AVRONE_LLM_BACKOFF", "0")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    gateway.HTTP_TRANSPORT = httpx.MockTransport(handler)
    return gateway.Gateway(Settings())


def teardown_function():
    gateway.HTTP_TRANSPORT = None


def body(obj, status=200):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return httpx.Response(status, text=text, headers={"content-type": "application/json"})


def run(monkeypatch, obj, retries="3"):
    hits = []
    def h(r):
        hits.append(1)
        return body(obj)
    g = mk(monkeypatch, h, retries)
    return g, hits


# --- 200 with an error body -------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    {"error": {"message": "model 'gpt-oss:120b' not found, try pulling it first"}},
    {"error": "model \"x\" not found"},
    {"error": {"message": "Invalid API key", "type": "auth"}},
    {"error": {"code": "model_not_found", "message": {"detail": "The model does not exist"}}},
])
def test_200_permanent_error_fails_fast_with_its_message(monkeypatch, err):
    g, hits = run(monkeypatch, err)
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert len(hits) == 1
    assert "bad response" not in str(e.value)


def test_200_transient_error_is_retried_and_named(monkeypatch):
    g, hits = run(monkeypatch, {"error": {"message": "server busy, model loading"}})
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert len(hits) == 3
    assert "model loading" in str(e.value)


def test_error_next_to_choices_is_ignored(monkeypatch):
    g, _ = run(monkeypatch, {"error": None, "choices": [{"message": {"content": "ok"}}]})
    assert asyncio.run(g.chat(MSG)) == "ok"


# --- other shapes -----------------------------------------------------------------------------

@pytest.mark.parametrize("obj,want", [
    ({"choices": [{"text": '{"final": "legacy"}', "finish_reason": "stop"}]}, '{"final": "legacy"}'),
    ({"choices": [{"delta": {"content": "chunk"}}]}, "chunk"),
    ({"model": "gpt-oss", "message": {"role": "assistant", "content": "native"}, "done": True}, "native"),
    ("\ufeff" + json.dumps({"choices": [{"message": {"content": "bom"}}]}), "bom"),
    ("\n  " + json.dumps({"choices": [{"message": {"content": "ws"}}]}) + "\n", "ws"),
])
def test_alternate_shapes(monkeypatch, obj, want):
    g, _ = run(monkeypatch, obj)
    assert asyncio.run(g.chat(MSG)) == want


def test_native_ollama_tool_calls_with_dict_arguments(monkeypatch):
    obj = {"message": {"role": "assistant", "content": "",
                       "tool_calls": [{"function": {"name": "shell", "arguments": {"cmd": "ls"}}}]},
           "done": True, "done_reason": "stop"}
    g, _ = run(monkeypatch, obj)
    assert parse_action(asyncio.run(g.chat(MSG))) == {"tool": "shell", "args": {"cmd": "ls"}}


def test_native_ollama_length_reason_fails_fast(monkeypatch):
    g, hits = run(monkeypatch, {"message": {"content": ""}, "done_reason": "length"})
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "finish_reason=length" in str(e.value) and len(hits) == 1


@pytest.mark.parametrize("obj", [{"choices": []}, {"choices": [None]}, {"choices": [{"message": "x"}]},
                                 [1, 2], "<html>proxy login</html>", {"foo": 1}])
def test_unusable_bodies_still_bad_response(monkeypatch, obj):
    g, hits = run(monkeypatch, obj, retries="2")
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "bad response" in str(e.value) and len(hits) == 2


# --- SSE --------------------------------------------------------------------------------------

def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def test_sse_content_is_joined(monkeypatch):
    s = sse({"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": '{"final": '}}]},
            {"choices": [{"delta": {"content": '"streamed"}'}, "finish_reason": "stop"}]})
    g, _ = run(monkeypatch, s)
    assert parse_action(asyncio.run(g.chat(MSG))) == {"final": "streamed"}


def test_sse_tool_call_fragments_are_joined(monkeypatch):
    s = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "shell", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "ls"}'}}]},
                          "finish_reason": "tool_calls"}]})
    g, _ = run(monkeypatch, s)
    assert parse_action(asyncio.run(g.chat(MSG))) == {"tool": "shell", "args": {"cmd": "ls"}}


def test_sse_cut_off_tool_call_never_runs(monkeypatch):
    s = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "shell",
                                                                             "arguments": '{"cmd": "rm -'}}]},
                          "finish_reason": "length"}]})
    g, _ = run(monkeypatch, s)
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "nothing ran" in str(e.value)


def test_sse_error_chunk_surfaces(monkeypatch):
    g, hits = run(monkeypatch, "data: " + json.dumps({"error": {"message": "model not found"}}) + "\n\n")
    with pytest.raises(LLMError) as e:
        asyncio.run(g.chat(MSG))
    assert "model not found" in str(e.value) and len(hits) == 1


def test_sse_with_junk_lines_and_comments():
    s = ": keepalive\n\nevent: x\ndata: not json\n\n" + sse({"choices": [{"delta": {"content": "ok"}}]})
    choice, msg = extract_choice(s)
    assert msg["content"] == "ok"


def test_sse_with_no_choices_falls_back_to_json_error():
    with pytest.raises(ValueError):
        extract_choice("data: {}\n\ndata: [DONE]\n\n")


# --- Retry-After ------------------------------------------------------------------------------

def test_retry_after_http_date():
    now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    when = format_datetime(now + timedelta(seconds=12), usegmt=True)
    assert retry_after_seconds(when, now.timestamp()) == pytest.approx(12, abs=0.01)


def test_retry_after_past_date_is_zero():
    now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert retry_after_seconds(format_datetime(now - timedelta(hours=1), usegmt=True), now.timestamp()) == 0.0


@pytest.mark.parametrize("v,want", [("5", 5.0), (" 2.5 ", 2.5), ("-3", 0.0), ("", None), (None, None),
                                    ("nan", None), ("soon", None), ("inf", float("inf"))])
def test_retry_after_values(v, want):
    assert retry_after_seconds(v) == want


def test_delay_caps_http_date_at_30s():
    far = format_datetime(datetime.now(timezone.utc) + timedelta(hours=2), usegmt=True)
    r = httpx.Response(429, headers={"retry-after": far})
    assert _delay(r, 0.0, 0) == 30.0
