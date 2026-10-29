import asyncio
import json
from types import SimpleNamespace

import pytest

from avrone import memstore
from avrone import tools_extra as tx
from avrone import tools_mempeek as mp
from avrone.tools import builtin_registry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Builder-3", depth=0)
    store = memstore.MemoryStore(tmp_path / "memory.jsonl")
    return SimpleNamespace(reg=reg, ctx=ctx, store=store, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "memory_peek", args))


def test_registered_with_schema(env):
    t = env.reg.get("memory_peek")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("memory_peek", {"limit": "x"})[0] == "bad_args"
    assert env.reg.check("memory_peek", {"limit": 3, "prefix": "a"}) is None


def test_empty(env):
    out = call(env)
    assert "namespace builder" in out and "no memories saved yet" in out


def test_own_namespace_only(env):
    env.store.save("builder", "plan", "ship friday")
    env.store.save("scout", "plan", "OTHER AGENT VALUE")
    env.store.merge(memstore.remote_namespace("edge1", "builder"),
                    [{"key": "remote", "value": "REMOTE VALUE", "ts": 1.0}])
    out = call(env)
    assert "1/" in out and "- plan" in out and "ship friday" in out
    assert "OTHER AGENT" not in out and "REMOTE" not in out
    # args cannot choose a namespace
    out = call(env, namespace="scout", ns="scout")
    assert "OTHER AGENT" not in out
    # a pinned namespace is honoured; a non-local pin falls back to the derived one
    env.ctx.memory_namespace = "scout"
    assert "OTHER AGENT VALUE" in call(env)
    env.ctx.memory_namespace = "fleet:edge1/builder"
    out = call(env)
    assert "REMOTE" not in out and "namespace builder" in out


def test_redaction(env):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    env.store.save("builder", "api_key", "plain-but-secret-key-name")
    env.store.save("builder", "notes", f"use {token} for pushes")
    env.store.save("builder", "db_url", "postgres://admin:hunter2@db:5432/x?password=pw123&x=1")
    env.store.save("builder", "ctl", "line1\nline2\x1b[31m")
    out = call(env, value_chars=500)
    assert "plain-but-secret" not in out and "api_key" in out and "***set***" in out
    assert token not in out and "ghp_" not in out and "token-shaped" in out
    assert "hunter2" not in out and "pw123" not in out and "postgres://***@db" in out
    assert "\x1b" not in out and "line1?line2" in out


def test_redaction_before_truncation(env):
    # a token cut mid-way must not leak its prefix through the preview
    token = "sk-" + "Z" * 40
    env.store.save("builder", "note", "x" * 5 + token)
    out = call(env, value_chars=10)
    assert "ZZZZ" not in out and "sk-" not in out


def test_limit_prefix_truncation_and_json(env):
    for i in range(30):
        env.store.save("builder", f"k{i:02d}", "v" * 300)
    env.store.save("builder", "other", "o")
    out = call(env, limit=5, prefix="k", value_chars=20)
    rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(rows) == 5 and "30 matching" in out and "25 more" in out
    assert all(r.endswith("v" * 20 + "...") for r in rows)
    assert "other" not in out
    rep = json.loads(call(env, limit=1000, format="json"))
    assert len(rep["shown"]) == 31  # limit clamped to PEEK_MAX_LIMIT (50) > 31 entries
    assert rep["entries_total"] == 31 and rep["value_bytes"] == 30 * 300 + 1
    keys_only = json.loads(call(env, value_chars=0, format="json"))
    assert all("value" not in e for e in keys_only["shown"])
    assert keys_only["newest"].endswith("Z")


def test_newest_first(env):
    env.store.save("builder", "a", "1")
    env.store.save("builder", "b", "2")
    env.store.save("builder", "a", "3")
    rep = json.loads(call(env, format="json"))
    assert [e["key"] for e in rep["shown"]] == ["a", "b"]
    assert rep["shown"][0]["value"] == "3"


@pytest.mark.parametrize("args,msg", [
    ({"limit": True}, "ERROR: limit"),
    ({"value_chars": "lots"}, "ERROR: limit"),
    ({"prefix": "p" * 300}, "ERROR: prefix"),
])
def test_bad_args(env, args, msg):
    assert call(env, **args).startswith(msg)


def test_output_capped(env):
    for i in range(50):
        env.store.save("builder", f"{'k' * 190}{i:02d}", "v" * 2000)
    out = call(env, limit=50, value_chars=500)
    assert len(out) <= mp.PEEK_MAX_CHARS + 20 and out.endswith("[truncated]")


def test_blob_redacted_but_plain_text_kept(env):
    env.store.save("builder", "hash", "commit aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3 done")
    env.store.save("builder", "prose", "a" * 100 + " plain words 12345")
    rep = json.loads(call(env, value_chars=500, format="json"))
    vals = {e["key"]: e["value"] for e in rep["shown"]}
    assert vals["hash"].startswith("***redacted")
    assert vals["prose"].endswith("plain words 12345")
