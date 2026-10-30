import asyncio
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import tools_extra as tx
from avrone import tools_lessonforget as lf
from avrone.memstore import MemoryStore
from avrone.tools import builtin_registry

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class Codex:
    def __init__(self):
        self.ev = []

    def emit(self, *a, **k):
        self.ev.append((a, k))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)), codex=Codex())
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", role="worker", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, core=core, tmp=tmp_path)


def call(env, name, **a):
    return asyncio.run(env.reg.call(env.ctx, name, a))


def learn(env, text):
    out = call(env, "research_note", text=text)
    assert "NEWLY LEARNED" in out
    return bm.lesson_key(text)


def test_memstore_delete(tmp_path):
    st = MemoryStore(tmp_path / "m.jsonl")
    st.save("ns", "a", "1")
    st.save("ns", "b", "2")
    assert st.delete("ns", "a") is True and st.delete("ns", "a") is False
    assert set(MemoryStore(tmp_path / "m.jsonl").load("ns")) == {"b"}


def test_registered(env):
    assert env.reg.check("lesson_forget", {"key": "l-0"})[0] == "bad_args"
    assert env.reg.check("bridge_status", {}) is None


def test_forget_round_trip(env):
    key = learn(env, "always pin numpy below 2")
    out = call(env, "lesson_forget", key=key, reason="numpy 2 works now")
    assert out.startswith(f"forgot lesson {key}") and "correction [" in out
    assert "always pin numpy" not in call(env, "lesson_recall", topic="numpy pin")
    corr = [p for p in bm.for_core(env.core, "run1").posts() if p["kind"] == "correction"]
    assert corr and "numpy 2 works now" in corr[0]["text"]
    assert any(a[1] == "lesson.forget" for a, _ in env.core.codex.ev)
    # learnable again later
    assert "NEWLY LEARNED" in call(env, "research_note", text="always pin numpy below 2")


@pytest.mark.parametrize("args,msg", [
    ({"key": "nope", "reason": "x"}, "key must look like"),
    ({"key": "l-0123456789abcdef", "reason": "  "}, "reason is required"),
    ({"key": "l-0123456789abcdef", "reason": "r" * 301}, "cap is 300"),
    ({"key": "l-0123456789abcdef", "reason": "gone"}, "no lesson"),
])
def test_refusals(env, args, msg):
    assert msg in call(env, "lesson_forget", **args)


def test_off_and_no_data_dir(env, monkeypatch):
    key = "l-0123456789abcdef"
    env.core.settings = SimpleNamespace(data_dir=None)
    assert "no data_dir" in call(env, "lesson_forget", key=key, reason="r")
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    assert "lessons are off" in call(env, "lesson_forget", key=key, reason="r")


def test_reason_redacted_and_failures_tolerated(env, monkeypatch):
    key = learn(env, "rotate the deploy token weekly")
    env.core.codex = SimpleNamespace(emit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(bm, "for_core", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    out = call(env, "lesson_forget", key=key, reason=f"leaked {SECRET}")
    assert "bridge post failed" in out and SECRET not in out


def test_forget_oserror(env, monkeypatch):
    monkeypatch.setattr(bm, "_store", lambda d: (_ for _ in ()).throw(OSError()))
    assert "could not update" in call(env, "lesson_forget", key="l-0123456789abcdef", reason="r")


def test_bridge_status(env):
    assert "no posts yet" in call(env, "bridge_status")
    b = bm.for_core(env.core, "run1")
    q = b.post("A-1", "question", f"why slow? {SECRET}")
    q2 = b.post("B-2", "question", "who owns the queue?")
    b.reply("B-2", [q["id"]], "fsync per claim")
    b.post("A-1", "conclusion", "batch fsync")
    b.merge_remote([{"id": "remote123456", "kind": "finding", "text": "edge says hi", "origin_node": "edge1"}], "edge1")
    out = call(env, "bridge_status")
    assert "5/" in out and "1 relayed" in out and "1 conclusion" in out
    assert "answer=1, conclusion=1, finding=1, question=2" in out
    assert "open questions (no reply yet): 1" in out and q2["id"] in out and q["id"] not in out.split("open")[1]
    assert SECRET not in out


def test_bridge_status_caps(env, monkeypatch):
    b = bm.for_core(env.core, "run1")
    for i in range(25):
        b.post(f"A-{i}", "finding", "x")
    monkeypatch.setattr(lf, "STATUS_MAX_CHARS", 60)
    out = call(env, "bridge_status")
    assert out.endswith("[truncated]")
    assert lf.bridge_status(env.ctx)["more_agents"] == 5
